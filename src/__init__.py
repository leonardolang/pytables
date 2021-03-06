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

MODULE_NAME = 'pytables'

def pytables_socket(mode):
    return b'\0' + b'{0}-{1}.server'.format(MODULE_NAME, mode)

def formatcall(this, name, args, kwargs, res=None):
    args = [ str(arg) for arg in args ]
    args.extend( [ '{k}={v!s}'.format(k=key,v=val) for (key, val) in kwargs.items() ] )

    return '{s!s}.{n}({a}){r}'.format(s=(this.__class__.__name__ if hasattr(this, '__class__') else this.__name__),
        n=name, a=', '.join(args), r=(' = {r!s}'.format(r=res[0]) if res is not None else ''))

def debugcall(method):
    name = method.__name__
    def wrapper(self, *args, **kwargs):
        IptcMain.logger.debug(formatcall(self, name, args, kwargs))
        res = method(self, *args, **kwargs)
        IptcMain.logger.debug(formatcall(self, name, args, kwargs, res=(res,)))
        return res
    wrapper.__name__ = method.__name__
    wrapper.__doc__  = method.__doc__
    return wrapper

class IPTCError(Exception):

    def __init__(self, s=None):
        super(IPTCError, self).__init__(s)
        pass


class XTablesError(Exception):

    def __init__(self, s=None):
        super(XTablesError, self).__init__(s)
        pass


class IptcCache():

    @classmethod
    @debugcall
    def load(cls, mode, lines, reloading=True, autoload=True):
        table = None

        if reloading:
            # clear rules
            for name, chain in Chain._cache.items():
                if chain.table.addrfamily != mode:
                    continue
                IptcMain.logger.debug('clearing chain {0}'.format(name))
                for rule in chain._rules:
                    IptcMain.logger.debug('marking rule {0} as invalid'.format(repr(rule)))
                    rule.valid = False
                del chain._rules[:]

            # initialize control attribute
            for _, chain in Chain._cache.items():
                if chain.table.addrfamily != mode:
                    continue
                chain.valid = False

        for line in lines:
            stripline = line.strip()

            if stripline.startswith('#'):
                continue

            if stripline.startswith('*'):
                IptcMain.logger.debug('found table specification "{0}"'.format(stripline))
                table = IptcBaseTable(stripline[1:], mode, autocommit=True, autoload=autoload)
                continue

            if stripline.startswith(':'):
                IptcMain.logger.debug('found chain specification "{0}"'.format(stripline))
                (chain_name, chain_policy, chain_stats) = shlex.split(stripline[1:])

                if table is None:
                    IptcMain.logger.error('no table, cannot create chain "{0}"'.format(chain_name))
                    continue

                if chain_policy == '-':
                    chain_policy = None

                chain = Chain(table, chain_name, policy=chain_policy, autoload=autoload)
                for c in table._chains:
                    if chain.name == c.name:
                        break
                else:
                    table._chains.append(chain)

                if reloading:
                    IptcMain.logger.debug('chain {0} is valid'.format(chain_name))
                    chain.valid = True
                continue

            if any((stripline.startswith(e) for e in [ '-A', '-D', '-I' ])):
                IptcMain.logger.debug('found rule specification "{0}"'.format(stripline))

                rdata = shlex.split(stripline)

                if table is None:
                    IptcMain.logger.error(
                        'no table, cannot load rule in chain "{0}"'.format(rdata[1] \
                            if len(rdata) <> 0 else '<none>'))
                    continue

                chain = Chain(table, rdata[1], autoload=autoload)

                if chain is None:
                    IptcMain.logger.error('no chain, cannot load rule "{0}"'.format(stripline))
                    continue

                try:
                    rulepos = None
                    datapos = 2

                    if rdata[0] in [ '-I', '-D'] and rdata[2].isdigit():
                        rulepos = int(rdata[2])
                        datapos = 3

                    rule = chain.deserialize(rdata[datapos:], valid=True)

                    IptcMain.logger.debug('running action "{0}" (@{1!s}) on rule "{2!s}"...'.format(rdata[0], rulepos, rule))

                    if   rdata[0] == '-A':
                        chain.append_rule(rule, autoload=autoload)
                    elif rdata[0] == '-I':
                        chain.insert_rule(rule, **(dict(autoload=autoload, pos=rulepos) if rulepos is not None else dict(autoload=autoload)))
                    elif rdata[0] == '-D':
                        chain.delete_rule(rule, **(dict(autoload=autoload, pos=rulepos) if rulepos is not None else dict(autoload=autoload)))
                    else:
                        IptcMain.logger.warning('unknown action "{0}", ignoring rule "{1!s}"'.format(rdata[0], rdata[1:]))

                except IPTCError, e:
                    IptcMain.logger.error('unable to parse chain {0}: {1!s}'.format(chain.name, e))
                    raise

                continue

        if reloading:
            # scan control attribute and remove chains not valid
            for _, table in IptcBaseTable._cache.items():
                if table.addrfamily != mode:
                    continue
                remchains = []
                for chain in table._chains:
                    if chain.valid == False:     # dont change this
                        remchains.append(chain)

                    remrules = []
                    for rule in chain.rules:
                        if rule.valid == False:  # dont change this
                            remrules.append(rule)
                    for rule in remrules:
                        chain.rules.remove(rule)

                for chain in remchains:
                    table._chains.remove(chain)

    @classmethod
    def save(cls, mode):
        IptcMain.logger.debug('IptcCache::save({0})'.format(mode))
        res = []
        for _, table in IptcBaseTable._cache.items():
            if table.addrfamily != mode:
                continue
            IptcMain.logger.debug('saving table {0}'.format(table.name))
            res.append('*' + table.name + '\n')
            res.extend(table.dump(eol='\n'))
        return res

class IptcMain():
    logger = None
    debug = None
    name = None

    @classmethod
    def setLogger(cls, logger):
        cls.logger = logger

    @classmethod
    def setDebug(cls, debug):
        cls.debug = debug
        if cls.logger is not None:
            cls.logger.setLevel(logging.DEBUG if debug else logging.INFO)

    @classmethod
    def setName(cls, name):
        cls.name = name

    @classmethod
    def getEnvironmentDebug(cls):
        rmap = { '0': False, '1': True, None: None }
        return rmap.get(os.environ.get('PYTABLES_DEBUG'))

    @classmethod
    def initialize(cls, name, debug=False, disk=None, console=False):
        logger = logging.getLogger(name)

        if disk:
            handler = logging.handlers.RotatingFileHandler(
                '/var/log/{name}.log'.format(name=name), maxBytes=3000000, backupCount=5)
            handler.setFormatter(
                logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s'.format(mod=name)))
            logger.addHandler(handler)

        if console:
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(
                logging.Formatter('{mod} %(levelname)s: %(message)s'.format(mod=name)))
            logger.addHandler(handler)

        if not disk and not console:
            logger.addHandler(logging.NullHandler())

        cls.setLogger(logger)
        cls.setDebug(debug)


# Internal base classes

class IptcBaseContainer(object):

    def __init__(self, kwargs=None):
        self.exclude = set(self.__dict__.keys())
        if kwargs is not None:
            for name, value in kwargs.items():
                setattr(self, name, value)

    def attributes(self, out):
        for key, val in self.__dict__.items():
            if key in self.exclude or key == 'exclude':
                continue
            param = key.replace('_', '-')

            IptcMain.logger.debug('adding attribute "{0}" = "{1}"...'.format(param, val))

            outarg = '--{p}'.format(p=param)
            if len(val) != 0:
                if val[0] == '!':
                    out.extend(['!', outarg, val[2:]])
                else:
                    out.extend([outarg, val])
            else:
                out.append(outarg)

        return out


class IptcChainHook(object):
    def __init__(self, obj, valid):
        self.obj, self.valid = obj, valid

    def run(self):
        IptcMain.logger.debug('HOOK: setting {name} valid = {0!s}'.format(self.valid,
            name=('Chain({0})'.format(self.obj.name) if hasattr(self.obj, 'name') \
                else '<{0}>'.format(repr(self.obj)))))
        self.obj.valid = self.valid


class IptcBaseTable(object):
    FILTER  = 'filter'
    NAT     = 'nat'
    MANGLE  = 'mangle'

    _managers = {}
    _manager = None
    _cache = {}

    @classmethod
    def getManager(cls, mode=None):
        if mode is None:
            if cls._manager is None:
                from client import Manager
                cls._manager = Manager
            return cls._manager
        else:
            if mode not in cls._managers:
                from client import Manager
                cls._managers[mode] = Manager.manager(mode)
            return cls._managers[mode]

    def __new__(cls, name, addrfamily, autocommit, autoload=True):
        if autoload:
            IptcBaseTable.getManager().initialize()
        refer = '{0}.{1}'.format(addrfamily, name)
        IptcMain.logger.debug(
            'checking cache for Table({0})'.format(refer))
        obj = IptcBaseTable._cache.get(refer, None)
        if obj is None:
            IptcMain.logger.debug(
                'no previous Table({0}) object, creating new...'.format(refer))
            obj = object.__new__(cls)
            obj.setup(name, addrfamily, autocommit)
            IptcBaseTable._cache[refer] = obj
        if autoload:
            IptcMain.logger.debug('requested autoload from Table({0})'.format(refer))
            IptcBaseTable.getManager(addrfamily).resync()
        return obj

    def setup(self, name, addrfamily, autocommit):
        self._chains = []
        self.name = name
        self.addrfamily = addrfamily
        self.autocommit = autocommit

    def manager(self):
        return IptcBaseTable.getManager(self.addrfamily)

    def update(self, data, hook=None):
        self.manager().update(self.name, data, hook=hook)
        if self.autocommit:
            IptcMain.logger.debug(
                'calling autocommit for {0} on update'.format(self.name))
            self.manager().save()

    chains = property(lambda s: list(s._chains))

    @debugcall
    def restart(self):
        self.manager().resync(force=True)

    @debugcall
    def resync(self):
        self.manager().resync()

    @debugcall
    def commit(self):
        self.manager().save()

    @debugcall
    def close(self):
        self.manager().close()

    def is_chain(self, chain):
        if isinstance(chain, str):
            chain = Chain(self, chain)

        if chain in self._chains:
            return chain.valid == True # could be None!

        return False

    @debugcall
    def create_chain(self, chain):
        if isinstance(chain, str):
            chain = Chain(self, chain)

        IptcMain.logger.debug('create_chain({0},valid={1!s})'.format(chain.name, chain.valid))

        for cur in self._chains:
            if chain.name == cur.name:
                break
        else:
            self._chains.append(chain)

        if chain.valid == True:  # really, dont change this
            IptcMain.logger.debug('chain {0} already valid, skipping insert cmd'.format(chain.name))
            return

        out = ['-N {name}'.format(name=chain.name)]

        self.manager().update(self.name, out, hook=IptcChainHook(chain, True))
        if self.autocommit:
            IptcMain.logger.debug(
                'calling autocommit for {0} on chain {1} creation'.format(self.name, chain.name))
            self.manager().save()
        return chain

    def delete_chain(self, chain):
        if isinstance(chain, str):
            chain = Chain(self, chain)

        IptcMain.logger.debug('delete_chain({0},valid={1!s})'.format(chain.name, chain.valid))

        for cur in self._chains:
            if chain.name == cur.name:
                self._chains.remove(cur)
                break

        if chain.valid == False:  # i'm serious, don't change it
            IptcMain.logger.debug('chain {0} not valid, skipping remove cmd'.format(chain.name))
            return

        out = ['-X {name}'.format(name=chain.name)]

        self.manager().update(self.name, out, hook=IptcChainHook(chain, False))
        if self.autocommit:
            IptcMain.logger.debug(
                'calling autocommit for {0} on chain {1} deletion'.format(self.name, chain.name))
            self.manager().save()

    @debugcall
    def load(self):
        self.manager.load()

    def dump(self, eol=''):
        res = []
        for chain in self._chains:
            pol = '-' if chain.policy is None else chain.policy
            res.append(':{0} {1} [0:0]{2}'.format(chain.name, pol, eol))
            res.extend(chain.dump(eol))
        return res

    def __str__(self):
        return 'Table({0}.{1})'.format(self.addrfamily, self.name)

# External interface starts here


class Table(IptcBaseTable):

    def __new__(self, name, autocommit=True):
        return super(Table, self).__new__(Table, name, 'ipv4', autocommit)


class Table6(IptcBaseTable):

    def __new__(self, name, autocommit=True):
        return super(Table6, self).__new__(Table6, name, 'ipv6', autocommit)


class Chain(object):
    _cache = {}

    def __new__(cls, table, name, policy=None, autoload=True):
        refer = '{0}.{1}.{2}'.format(table.addrfamily, table.name, name)
        IptcMain.logger.debug('checking cache for Chain({0})'.format(refer))
        obj = Chain._cache.get(refer, None)
        if obj is None:
            IptcMain.logger.debug('no previous Chain object, creating new...')
            obj = object.__new__(cls)
            obj.setup(table, name, policy)
            Chain._cache[refer] = obj
        if autoload:
            IptcMain.logger.debug('requested autoload from Chain({0})'.format(refer))
            from client import Manager
            Manager.manager(table.addrfamily).resync()
        return obj

    def setup(self, table, name, policy):
        IptcMain.logger.debug('running init on Chain {0}...'.format(name))
        self._rules = []
        self.table = table
        self.name = name
        self.policy = policy
        self.valid = None

    def deserialize(self, rdata, valid=False):
        attrmap = {
            '-s': 'src', '--src': 'src',
            '-d': 'src', '--dst': 'dst',
            '-i': 'in_interface',  '--in-interface':  'in_interface',
            '-o': 'out_interface', '--out-interface': 'out_interface',
            '-p': 'protocol', '--protocol': 'protocol', '--proto': 'protocol'
        }

        objopts = ['-m', '-j', '-g']

        IptcMain.logger.debug('processing rule data: {0!s}'.format(rdata))

        rule = Rule(valid=valid)

        optind = 0
        revopt = False

        def rev_value(revset, value):
            return '! {0}'.format(value) if revset else value

        while optind < len(rdata):
            IptcMain.logger.debug('processing arg "{0}"'.format(rdata[optind]))
            attrdata = attrmap.get(rdata[optind])

            if attrdata is not None:
                if (optind + 1) == len(rdata):
                    raise IPTCError('missing value for option "{0}"'.format(attrdata))

                value = rev_value(revopt, rdata[optind + 1])
                IptcMain.logger.debug('setting rule attr "{0}" = "{1}"'.format(attrdata, value))
                setattr(rule, attrdata, value)
                optind += 2
                revopt = False
                continue

            elif rdata[optind] in objopts:
                if (optind + 1) == len(rdata):
                    raise IPTCError('missing name for match/target/goto {0}'.format(rdata[optind]))

                offtind = optind + 2
                nextind = len(rdata) - offtind
                for nextopt in objopts:
                    try:
                        tmpind = rdata[offtind:].index(nextopt)
                        if tmpind < nextind:
                            nextind = tmpind
                    except:
                        pass

                nextind += offtind

                if rdata[optind] == '-m':
                    obj = Match(rule, rdata[optind + 1], reverse=revopt)
                elif rdata[optind] == '-j':
                    obj = Target(rule, rdata[optind + 1])
                else:
                    obj = Goto(rule, rdata[optind + 1])

                IptcMain.logger.debug('found {0} {1}{2} ({3}:{4})'.format(obj.__class__.__name__,
                    ('! ' if revopt else ''), rdata[optind + 1], optind, nextind))

                revopt = False
                argind = offtind

                while argind < nextind:
                    arg = rdata[argind]
                    if arg.startswith('--'):
                        param = arg[2:].replace('-', '_')

                        value = list()
                        argind += 1
                        while argind < nextind and not rdata[argind].startswith('--'):
                            value.append(rdata[argind])
                            argind += 1

                        value = rev_value(revopt, ' '.join(value))
                        IptcMain.logger.debug('setting object attribute "{0}" to "{1}"'.format(param, value))
                        setattr(obj, param, value)
                        revopt = False

                    elif arg == '!':
                        revopt = True
                        argind += 1

                    else:
                        IptcMain.logger.error('unknown argument {0} in {1!s}, skipping'.format(arg, rdata))
                        argind += 1

                if rdata[optind] == '-m':
                    rule.add_match(obj)
                else:
                    rule.target = obj

                optind = nextind
                revopt = False
                continue

            elif rdata[optind] == '!':
                revopt = True
                optind += 1

            else:
                raise IPTCError('unable to process option {0}'.format(rdata[optind]))

        return rule

    def rules(self):
        return self._rules

    rules = property(lambda s: list(s._rules))

    def insert_rule(self, rule, pos=1, autoload=True):
        IptcMain.logger.debug('inserting rule {0!s}, pos {1!s}'.format(rule, pos))

        tmp = []
        if rule not in self._rules:
            if pos is None:
                self._rules.append(rule)
                tmp.extend(['-A', self.name])
            else:
                self._rules.insert(pos-1, rule)
                tmp.extend(['-I', self.name, str(pos)])
        else:
            pos = self._rules.index(rule)
            tmp.extend(['-I', self.name, pos])

        if autoload:
            tmp.append(rule.serialize())
            res = [' '.join(tmp)]
            IptcMain.logger.debug('saving Chain({0})'.format(res))
            self.table.update(res, hook=IptcChainHook(rule, True))

    def append_rule(self, rule, autoload=True):
        return self.insert_rule(rule, pos=None, autoload=autoload)

    def delete_rule(self, rule, pos=None, autoload=True):
        IptcMain.logger.debug('deleting rule {0!s}, pos {1!s}'.format(rule, pos))

        if rule in self._rules:
            self._rules.remove(rule)

        if rule.valid == False:  # i'm serious, don't change it
            IptcMain.logger.debug('rule not valid, skipping remove command')
            return

        tmp = [ '-D', self.name, rule.serialize() if pos is None else str(pos) ]

        if autoload:
            res = [' '.join(tmp)]
            IptcMain.logger.debug('deleting: {0!s}'.format(res))
            self.table.update(res, hook=IptcChainHook(rule, False))

    def flush(self):
        self._rules = []
        self.table.update(['-F {0}'.format(self.name)])

    def dump(self, eol=''):
        res = []
        for rule in self._rules:
            res.append('-A {0} {1}{2}'.format(self.name, rule.serialize(), eol))
        return res

    def __str__(self):
        return 'Chain({0}.{1}.{2})'.format(self.table.addrfamily, self.table.name, self.name)


class Rule(IptcBaseContainer):

    def __init__(self, **kwargs):
        self.target = None
        self.matches = []
        self.valid = kwargs.pop('valid', None)

        # this should be the last line in init
        super(Rule, self).__init__(kwargs=kwargs)

    def serialize(self):
        out = self.attributes([])
        for m in self.matches:
            out.append(m.serialize())
        if self.target is not None:
            out.append(self.target.serialize())
        res = ' '.join(out)
        IptcMain.logger.debug('serialize Rule({0})'.format(res))
        return res

    def add_match(self, match):
        self.matches.append(match)
        match.rule = self

    def create_target(self, chain_name):
        self.target = Target(self, chain_name)
        self.target.rule = self
        return self.target

class Rule6(Rule):
    pass


class Match(IptcBaseContainer):

    def __init__(self, rule, name, reverse=False, **kwargs):
        self.rule = rule
        self.name = name
        self.reverse = reverse

        if rule is not None:
            rule.add_match(self)

        # this should be the last line in init
        super(Match, self).__init__(kwargs=kwargs)

    def serialize(self):
        lst = ['-m', self.name]
        if self.reverse:
            lst.insert(0, '!')
        res = ' '.join(self.attributes(lst))
        IptcMain.logger.debug('serialize Match({0})'.format(res))
        return res


class Target(IptcBaseContainer):

    def __init__(self, rule, name, **kwargs):
        self.rule = rule
        self.name = name

        tmpname = str(name).upper()
        self.standard = tmpname != name

        if rule is not None:
            rule.target = self

        # this should be the last line in init
        super(Target, self).__init__(kwargs=kwargs)

    def serialize(self):
        lst = ['-j', self.name]
        res = ' '.join(self.attributes(lst))
        IptcMain.logger.debug('Target({0})'.format(res))
        return res


class Goto(IptcBaseContainer):

    def __init__(self, rule, name, **kwargs):
        self.rule = rule
        self.name = name

        tmpname = str(name).upper()
        self.standard = tmpname != name

        rule.target = self

        # this should be the last line in init
        super(Goto, self).__init__(kwargs=kwargs)

    def serialize(self):
        lst = ['-g', self.name]
        res = ' '.join(self.attributes(lst))
        IptcMain.logger.debug('Goto({0})'.format(res))
        return res


def dump_current_cache():
    '''Debug function, dumps all the current cache in memory.'''

    IptcMain.logger.info('-------- DUMPING CACHE BEGIN --------')
    for keyname, table in IptcBaseTable._cache.items():
        IptcMain.logger.info('TABLE({0} {1} [{2}]'.format(table.addrfamily, table.name, keyname))

        for chain in table._chains:
            IptcMain.logger.info('  CHAIN {0} {1!s}'.format(chain.name, chain.policy))

            for rule in chain._rules:
                IptcMain.logger.info('    RULE: {0}'.format(rule.serialize()))

    IptcMain.logger.info('--------- DUMPING CACHE END ---------')
