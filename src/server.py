# Copyright (C) 2014  Sangoma Technologies Corp.
# All Rights Reserved.
#
# Author(s)
# Leonardo Lang <lang@sangoma.com>
#
# This modules provides a mostly IPTC-compatible interface which
# uses ip{,6}tables-{save,restore} for loading/saving rules.

import os
import sys
import shlex
import logging
import logging.handlers
import subprocess
import socket
import traceback
import multitask as mt
import errno
import struct
import time
import fcntl

from . import IptcMain, IptcCache, IPTCError, pytables_socket

import ConfigParser

MODULE_NAME = 'pytables-server'
CONFIG_NAME = '/etc/pytables/server.conf'

class WorkerInstance(object):

    def __init__(self, mode, save=None, load=None):
        self.mode = mode
        self.cmdsave = save
        self.cmdload = load
        self.failed = False
        self.loaded = False
        self.proc = None
        self.line = 0

    def restart(self):
        self.close()
        self.load()
        self.start()

    def load(self):
        if self.loaded:
            return True
        IptcMain.logger.debug(
            'worker loading from "{name}"...'.format(name=self.cmdload[0]))
        try:
            loadproc = subprocess.Popen(
                self.cmdload, bufsize=0, stdout=subprocess.PIPE)
            data = loadproc.communicate()[0].splitlines()
            IptcCache.load(self.mode, data, autoload=False)
        except:
            return str(sys.exc_info()[1])
        return None

    def start(self):
        if self.proc is not None:
            return

        IptcMain.logger.debug('restarting process "{0}"...'.format(self.cmdsave[0]))

        try:
            self.proc = subprocess.Popen(self.cmdsave, bufsize=0, stdin=subprocess.PIPE)
        except Exception as e:
            raise IPTCError('unable to spawn "{0}": {1!s}'.format(self.cmdsave[0], e))

    def close(self, failed=False):
        self.line = 0

        self.failed = failed
        self.loaded = False

        if self.proc is None:
            return

        IptcMain.logger.debug('closing process "{name}" (pid={pid})...'.format(
            name=self.cmdsave[0], pid=self.proc.pid))

        self.proc.stdin.flush()
        self.proc.stdin.close()
        retcode = self.proc.wait()

        if retcode != 0:
            IptcMain.logger.info(
                'process "{name}" returned {ret}'.format(name=self.cmdsave[0], ret=retcode))

        self.proc = None

    def save(self, data):
        self.start()

        IptcMain.logger.debug(
            'worker({mode}) storing rules...'.format(mode=self.mode))

        def poutput(s, flush=False, dup=None):
            self.line += 1
            IptcMain.logger.debug('worker({mode},{line:03d}) writing: {data}'.format(
                mode=self.mode, line=self.line, data=s))
            sdata = s + '\n'
            self.proc.stdin.write(sdata)
            if dup is not None:
                dup.append(sdata)
            if flush:
                self.proc.stdin.flush()
            return sdata

        duplines = []
        for tname, lines in data.items():
            try:
                poutput('*' + tname, dup=duplines)
                for ln in lines:
                    poutput(ln, dup=duplines)
                poutput('COMMIT', flush=True, dup=duplines)
                poutput('# COMMIT VALIDATION', flush=True)

            except:
                self.close(failed=True)
                return str(sys.exc_info()[1])

        # now apply changes to current cache
        IptcMain.logger.debug('worker({0}) loading changes...'.format(self.mode))
        IptcCache.load(self.mode, duplines, reloading=False, autoload=False)
        IptcMain.logger.debug('worker({0}) done'.format(self.mode))

        return None


class Worker():
    WORKERS = {
        'ipv4': WorkerInstance('ipv4', save=['/sbin/iptables-restore', '-n'], load=['/sbin/iptables-save']),
        'ipv6': WorkerInstance('ipv6', save=['/sbin/ip6tables-restore', '-n'], load=['/sbin/ip6tables-save'])
    }

    @classmethod
    def worker(cls, mode):
        return Worker.WORKERS.get(mode)


class ConnectionBaseState(object):

    def __init__(self):
        self.transitions = None

    def load(self, states):
        self.logdebug('no transitions loaded for this state')
        self.transitions = {}

    def handle(self, c, msg):
        raise StopIteration(False)
        yield

    def running(self, c):
        return
        yield

    def process(self, c, msg):
        if self.transitions is None:
            self.load(c.state)

        self.logdebug('calling handler', c)
        ret = yield self.handle(c, msg)

        m = self.transitions.get(msg)

        if m is not None:
            self.logdebug('transition to state "{0}"'.format(m.__class__.__name__), c)
            c.state.current = m
            yield m.running(c)

        raise StopIteration(ret)

    def logdebug(self, msg, c=None):
        IptcMain.logger.debug('{0}{1} {2}'.format(self.__class__.__name__,
            ('' if c is None else '{0!s} '.format(c.pid)), msg))

class ConnectionStateVoid(ConnectionBaseState):

    def __init__(self):
        super(ConnectionStateVoid, self).__init__()

    def load(self, states):
        self.transitions = {
            'LOAD': states.load,
            'SYNC': states.load,
            'BOOT': states.boot
        }

    def handle(self, c, msg):
        retr = False
        if msg == 'SAVE':
            yield c.send('FAILURE/current state is out-of-date')
            retr = True

            self.logdebug('handle(SAVE) = FAILURE', c)

        raise StopIteration(retr)


class ConnectionStateSync(ConnectionBaseState):

    def __init__(self):
        super(ConnectionStateSync, self).__init__()

    def load(self, states):
        self.transitions = {
            'SYNC': states.done,
            'LOAD': states.load,
            'SAVE': states.save,
            'BOOT': states.boot,
        }


class ConnectionStateLoad(ConnectionBaseState):

    def __init__(self):
        super(ConnectionStateLoad, self).__init__()

    def running(self, c):
        ret = Worker.worker(c.mode).load()

        if ret is not None:
            res = 'FAILURE/' + ret
        else:
            data = IptcCache.save(c.mode)

            self.logdebug('running(), sending {0!s} lines'.format(len(data)), c)
            yield c.sendbuffer(data, nl=False)
            res = 'OK'

        yield c.send(res)

        c.state.current = c.state.sync
        raise StopIteration()


class ConnectionStateSave(ConnectionBaseState):

    def __init__(self):
        self.data = {}
        self.curr = None
        super(ConnectionStateSave, self).__init__()

    def load(self, states):
        self.transitions = {
            'COMMIT': states.sync
        }

    def handle(self, c, msg):
        retr = False
        if msg == 'COMMIT':
            ret = Worker.worker(c.mode).save(self.data)
            res = 'OK' if ret is None else 'FAILURE/{ret}'.format(ret=ret)

            self.logdebug('handle(COMMIT) = {0}'.format(res))
            yield c.send(res)

            self.data = {}
            retr = True

        elif msg.startswith('TABLE/'):
            self.curr = msg[6:]
            self.data[self.curr] = []

        elif self.curr is not None:
            self.data[self.curr].append(msg)

        raise StopIteration(retr)
        yield

    def running(self, c):
        self.data = {}
        self.curr = None
        raise StopIteration()
        yield

class ConnectionStateDone(ConnectionBaseState):

    def __init__(self):
        super(ConnectionStateDone, self).__init__()

    def running(self, c):
        yield c.send('OK')
        c.state.current = c.state.sync
        raise StopIteration()


class ConnectionStateBoot(ConnectionBaseState):

    def __init__(self):
        super(ConnectionStateBoot, self).__init__()

    def running(self, c):
        worker = Worker.worker(c.mode)
        worker.close()
        worker.load()

        yield c.send('OK')
        c.state.current = c.state.void
        raise StopIteration()

class ConnectionState():

    def __init__(self):
        self.void = ConnectionStateVoid()
        self.sync = ConnectionStateSync()
        self.load = ConnectionStateLoad()
        self.save = ConnectionStateSave()
        self.done = ConnectionStateDone()
        self.boot = ConnectionStateBoot()
        self.current = self.void


class Connection():

    def __init__(self, mode, conn, pid):
        self.mode = mode
        self.stream = mt.Stream(conn)
        self.pid = pid
        self.state = ConnectionState()
        self.reply = 0
        self.logdebug('new client instance')

    def logdebug(self, msg):
        IptcMain.logger.debug('client({0},{1}) {2}'.format(self.mode, self.pid, msg))

    def send(self, data):
        if IptcMain.logger.isEnabledFor(logging.DEBUG):
            self.logdebug('sending: {0}'.format(data))
        yield self.sendbuffer(data)

    def sendformat(self, msg):
        ret = '{n:03x} {s}'.format(s=msg,n=self.reply)
        self.reply = (self.reply + 1) % 0x1000
        return ret

    def sendbuffer(self, data, nl=True):
        newline = '\n' if nl else ''
        data = map(self.sendformat, data) if isinstance(data, list) else self.sendformat(data)
        strdata, strlines = (newline.join(data) + newline, len(data)) \
            if isinstance(data, list) else (data + newline, 1)
        self.logdebug('sending {0!s} line(s) of data'.format(strlines))
        yield self.stream.write(strdata)

    def process(self, message, daemon):
        if (yield self.state.current.process(self, message)):
            daemon.reloaded(self)

    def run(self, daemon):
        self.logdebug('client running')
        try:
            while True:
                self.logdebug('waiting for data...')
                data = yield self.stream.read_until(ch='\n')
                if data is None:
                    break  # log something?
                if IptcMain.logger.isEnabledFor(logging.DEBUG):
                    self.logdebug('processing message: {0}'.format(data))
                msgdata = data.split(' ', 1)
                if len(msgdata) == 2:
                    yield self.process(msgdata[1], daemon)
                else:
                    IptcMain.logger.warning('discarding message with wrong format: {0}'.format(data))

        except socket.error as e:
            if e[0] != errno.EBADF and e[0] != errno.ECONNRESET and e[0] != errno.EPIPE:
                raise
        finally:
            self.logdebug('bailing out...')
            daemon.disconnect(self)

        raise StopIteration()


class ServerAlreadyRunning(Exception):
    pass


class Server():
    @classmethod
    def create(cls, mode):
        try:
            srv = Server(mode)
            srv.execute()
            return srv
        except ServerAlreadyRunning:
            IptcMain.logger.info('daemon already running, not starting')
        except Exception as e:
            IptcMain.logger.warning('could not start daemon: {0!s}'.format(e))
        return None

    @classmethod
    def getEnvironmentDebug(cls):
        return IptcMain.getEnvironmentDebug()

    @classmethod
    def initialize(cls, mode=None, debug=None, disk=None, console=None, partial=False):
        disk = True if disk is None else disk
        console = False if console is None else console

        config = ConfigParser.SafeConfigParser()

        try:
            if partial:
                raise Exception()

            if len(config.read(CONFIG_NAME)) == 0:
                raise Exception()

            sections = config.sections()

            def safeget(sec, name, defvalue, conv):
                try:
                    return conv(config.get(sec, name))
                except:
                    return defvalue

            secname = mode if mode is not None and mode in sections else 'default'

            def tobool(data):
                data = data.lower()
                try:
                    return bool(int(data))
                except:
                    return data == 'true' or data == 'yes' or data == 'y'

            debug = safeget(secname, 'debug', debug, tobool)
            disk = safeget(secname, 'disk',  disk, tobool)
            console = safeget(secname, 'console', console, tobool)
        except:
            pass

        debug = cls.getEnvironmentDebug() if debug is None else debug

        suffix = '-{0}'.format(mode) if mode is not None else ''
        IptcMain.initialize('{0}{1}'.format(MODULE_NAME, suffix), debug=debug,
            disk=disk, console=console)

    @classmethod
    def setupSocket(cls, mode):
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM, 0)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(pytables_socket(mode))
            sock.listen(5)
            sock.setblocking(0)
            return sock
        except socket.error as e:
            if e[0] == errno.EADDRINUSE:
                raise ServerAlreadyRunning()
            raise

    @classmethod
    def logger(cls):
        return IptcMain.logger

    def __init__(self, mode):
        self.sock = Server.setupSocket(mode)
        self.mode = mode
        self.tasks = None
        self.with_timeout = None

    def execute(self):
        pid = os.fork()

        if pid == 0:
            pid2 = os.fork()

            if pid2 == 0:
                try:
                    nilrd = open(os.devnull)
                    nilwr = open(os.devnull, 'w')
                    os.dup2(nilrd.fileno(), 0)
                    os.dup2(nilwr.fileno(), 1)
                    os.dup2(nilwr.fileno(), 2)
                    os.setsid()
                    # now setup and run
                    Server.initialize(self.mode)
                    os._exit(self.main())
                except:
                    traceback.print_exc(file=sys.stderr)
                    os._exit(123)
            else:
                os._exit(0)
        else:
            os.close(self.sock.fileno())
            os.waitpid(pid, 0)

    def cloexec(self, sock):
        sockflags = fcntl.fcntl(sock, fcntl.F_GETFD)
        fcntl.fcntl(sock, fcntl.F_SETFD, sockflags | fcntl.FD_CLOEXEC)

    def log(self, msg, debug=False):
        fn = IptcMain.logger.debug if debug else IptcMain.logger.info
        fn('server({0},{1}) {2}'.format(self.mode, os.getpid(), msg))

    def logdebug(self, msg):
        self.log(msg, debug=True)

    def main(self):
        self.cloexec(self.sock)
        mt.add(self.run())
        mt.run()
        return 0

    def run(self, enable_timeout=True):
        self.with_timeout = enable_timeout
        self.clients = set()

        self.log('listening...')
        try:
            while True:
                kwargs = dict()
                if len(self.clients) == 0 and enable_timeout:
                    kwargs.update(timeout=5)

                conn, addr = (yield mt.accept(self.sock, **kwargs))

                # socket.SO_PEERCRED = 17, sizeof(struct ucred) = 24
                buffdata = conn.getsockopt(socket.SOL_SOCKET, 17, 24)
                (pid, uid, gid) = struct.unpack('III', buffdata)

                self.log('connection from PID {0!s} (uid={1!s}, gid={2!s})'.format(pid, uid, gid))

                client = Connection(self.mode, conn, pid)
                self.connect(client, conn)
                mt.add(client.run(self))
        except socket.error as e:
            if e[0] != errno.EBADF:
                raise
        except mt.Timeout:
            self.log('timeout waiting for clients')
        finally:
            self.log('terminated')
            self.cleanup()

    def reloaded(self, client):
        self.logdebug('reload request from client PID {0!s}'.format(client.pid))
        for oclient in self.clients:
            if client == oclient:
                continue
            oclient.state.current = client.state.void

    def connect(self, client, conn):
        self.clients.add(client)
        # avoid issues with stuck descriptors
        self.cloexec(conn)

    def disconnect(self, client):
        if client in self.clients:
            self.clients.remove(client)

        if len(self.clients) == 0 and self.with_timeout:
            self.log('no clients left, starting timeout')

    def cleanup(self):
        for client in self.clients:
            try:
                client.stream.val.shutdown(socket.SHUT_RDWR)
            except:
                pass

        try:
            self.sock.close()
        except:
            pass
