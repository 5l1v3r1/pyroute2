'''
Structure and API
=================

The NDB objects are dictionary-like structures that represent network
objects -- interfaces, routes, addresses etc. In addition to the
usual dictionary API they have some NDB-specific methods, see the
`RTNL_Object` class description below.

The dictionary fields represent RTNL messages fields and NLA names,
and the objects are used as argument dictionaries to normal `IPRoute`
methods like `link()` or `route()`. Thus everything described for
the `IPRoute` methods is valid here as well.

See also: :ref:`iproute`

E.g.::

    # create a vlan interface with IPRoute
    with IPRoute() as ipr:
        ipr.link("add",
                 ifname="vlan1108",
                 kind="vlan",
                 link=ipr.link_lookup(ifname="eth0"),
                 vlan_id=1108)

    # same with NDB:
    with NDB(log="stderr") as ndb:
        (ndb
         .interfaces
         .create(ifname="vlan1108",
                 kind="vlan",
                 link="eth0",
                 vlan_id=1108)
         .commit())

Slightly simplifying, if a network object doesn't exist, NDB will run
an RTNL method with "add" argument, if exists -- "set", and to remove
an object NDB will call the method with "del" argument.

Accessing objects
=================

NDB objects are grouped into "views":

    * interfaces
    * addresses
    * routes
    * neighbours
    * rules
    * netns
    * ...

Views are dictionary-like objects that accept strings or dict selectors::

    # access eth0
    ndb.interfaces["eth0"]

    # access eth0 in the netns test01
    ndb.sources.add(netns="test01")
    ndb.interfaces[{"target": "test01", "ifname": "eth0"}]

    # access a route to 10.4.0.0/24
    ndb.routes["10.4.0.0/24"]

    # same with a dict selector
    ndb.routes[{"dst": "10.4.0.0", "dst_len": 24}]

Objects cache
=============

NDB create objects on demand, it doesn't create thousands of route objects
for thousands of routes by default. The object is being created only when
accessed for the first time, and stays in the cache as long as it has any
not committed changes. To inspect cached objects, use views' `.cache`::

    >>> ndb.interfaces.cache.keys()
    [(('target', u'localhost'), ('tflags', 0), ('index', 1)),  # lo
     (('target', u'localhost'), ('tflags', 0), ('index', 5))]  # eth3

There is no asynchronous cache invalidation, the cache is being cleaned up
every time when an object is accessed.
'''
import json
import time
import errno
import weakref
import traceback
import threading
import collections
from functools import partial
from pyroute2 import cli
from pyroute2.ndb.events import State
from pyroute2.ndb.report import Record
from pyroute2.netlink.exceptions import NetlinkError
from pyroute2.ndb.events import InvalidateHandlerException


class RTNL_Object(dict):
    '''
    Base RTNL object class.
    '''

    view = None    # (optional) view to load values for the summary etc.
    utable = None  # table to send updates to
    table_alias = ''

    key_extra_fields = []
    hidden_fields = []
    fields_cmp = {}

    schema = None
    event_map = None
    state = None
    log = None
    summary = None
    summary_header = None
    dump = None
    dump_header = None
    errors = None
    msg_class = None
    reverse_update = None
    _table = None
    _apply_script = None
    _key = None
    _replace = None
    _replace_on_key_change = False

    # 8<------------------------------------------------------------
    #
    # Documented public properties section
    #
    @property
    def table(self):
        '''
        The main reference table for the object. The SQL schema of the
        table is used to build the key and to verify the fields.
        '''
        return self._table

    @table.setter
    def table(self, value):
        self._table = value

    @property
    def etable(self):
        '''
        The table where the object actually fetches the data from. It
        is not always equal `self.table`, e.g. snapshot objects fetch
        the data from snapshot tables.

        Read-only property.
        '''
        if self.ctxid:
            return '%s_%s' % (self.table, self.ctxid)
        else:
            return self.table

    @property
    def key(self):
        '''
        The key of the object, used to fetch it from the DB.
        '''
        nkey = self._key or {}
        ret = collections.OrderedDict()
        for name in self.kspec:
            kname = self.iclass.nla2name(name)
            if kname in self:
                value = self[kname]
                if value is None and name in nkey:
                    value = nkey[name]
                ret[name] = value
        if len(ret) < len(self.kspec):
            for name in self.key_extra_fields:
                kname = self.iclass.nla2name(name)
                if self.get(kname):
                    ret[name] = self[kname]
        return ret

    @key.setter
    def key(self, k):
        if not isinstance(k, dict):
            return
        for key, value in k.items():
            if value is not None:
                dict.__setitem__(self, self.iclass.nla2name(key), value)

    #
    # 8<------------------------------------------------------------
    #

    def __init__(self,
                 view,
                 key,
                 iclass,
                 ctxid=None,
                 match_src=None,
                 match_pairs=None):
        self.view = view
        self.ndb = view.ndb
        self.sources = view.ndb.sources
        self.ctxid = ctxid
        self.schema = view.ndb.schema
        self.match_src = match_src or tuple()
        self.match_pairs = match_pairs or dict()
        self.changed = set()
        self.iclass = iclass
        self.utable = self.utable or self.table
        self.errors = []
        self.log = self.ndb.log.channel('rtnl_object')
        self.log.debug('init')
        self.state = State()
        self.state.set('invalid')
        self.snapshot_deps = []
        self.load_event = threading.Event()
        self.load_event.set()
        self.lock = threading.Lock()
        self.kspec = self.schema.compiled[self.table]['idx']
        self.knorm = self.schema.compiled[self.table]['norm_idx']
        self.spec = self.schema.compiled[self.table]['all_names']
        self.names = self.schema.compiled[self.table]['norm_names']
        self._apply_script = []
        if isinstance(key, dict):
            self.chain = key.pop('ndb_chain', None)
            create = key.pop('create', False)
        else:
            self.chain = None
            create = False
        ckey = self.complete_key(key)
        if create and ckey is not None:
            raise KeyError('object exists')
        elif not create and ckey is None:
            raise KeyError('object does not exists')
        elif create:
            for name in key:
                self[name] = key[name]
            # FIXME -- merge with complete_key()
            if 'target' not in self:
                self.load_value('target', self.view.default_target)
        elif ctxid is None:
            self.key = ckey
            self.load_sql()
        else:
            self.key = ckey
            self.load_sql(table=self.table)

    def mark_tflags(self, mark):
        pass

    def keys(self):
        return filter(lambda x: x not in self.hidden_fields,
                      dict.keys(self))

    def items(self):
        return filter(lambda x: x[0] not in self.hidden_fields,
                      dict.items(self))

    @classmethod
    def adjust_spec(cls, spec, context):
        return spec

    @classmethod
    def nla2name(self, name):
        return self.msg_class.nla2name(name)

    @classmethod
    def name2nla(self, name):
        return self.msg_class.name2nla(name)

    @property
    def context(self):
        return {'target': self.get('target', 'localhost')}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.commit()

    def __hash__(self):
        return id(self)

    def __getitem__(self, key):
        if key in self.match_pairs:
            for src in self.match_src:
                try:
                    return src[self.match_pairs[key]]
                except:
                    pass
        return dict.__getitem__(self, key)

    def __setitem__(self, key, value):
        if self.state == 'system' and key in self.knorm:
            if self._replace_on_key_change:
                self.log.debug('prepare replace for key %s' % (self.key))
                self._replace = type(self)(self.view, self.key)
                self.state.set('replace')
            else:
                raise ValueError('attempt to change a key field (%s)' % key)
        if key in ('net_ns_fd', 'net_ns_pid'):
            self.state.set('setns')
        if value != self.get(key, None):
            if key != 'target':
                self.changed.add(key)
            dict.__setitem__(self, key, value)

    def fields(self, *argv):
        Fields = collections.namedtuple('Fields', argv)
        return Fields(*[self[key] for key in argv])

    def key_repr(self):
        return repr(self.key)

    @cli.change_pointer
    def create(self, **spec):
        spec['create'] = True
        spec['ndb_chain'] = self
        return self.view[spec]

    @cli.show_result
    def show(self, **kwarg):
        '''
        Return the object in a specified format. The format may be
        specified with the keyword argument `format` or in the
        `ndb.config['show_format']`.

        TODO: document different formats
        '''
        fmt = kwarg.pop('format',
                        kwarg.pop('fmt',
                                  self.view.ndb.config.get('show_format',
                                                           'native')))
        if fmt == 'native':
            return dict(self)
        else:
            out = collections.OrderedDict()
            for key in sorted(self):
                out[key] = self[key]
            return '%s\n' % json.dumps(out, indent=4, separators=(',', ': '))

    def set(self, key, value):
        '''
        Set a field specified by `key` to `value`, and return self. The
        method is useful to write call chains like that::

            (ndb
             .interfaces["eth0"]
             .set('mtu', 1200)
             .set('state', 'up')
             .set('address', '00:11:22:33:44:55')
             .commit())
        '''
        self[key] = value
        return self

    def wtime(self, itn=1):
        return max(min(itn * 0.1, 1),
                   self.view.ndb._event_queue.qsize() / 10)

    def register(self):
        #
        # Construct a weakref handler for events.
        #
        # If the referent doesn't exist, raise the
        # exception to remove the handler from the
        # chain.
        #
        def wr_handler(wr, fname, *argv):
            try:
                return getattr(wr(), fname)(*argv)
            except:
                # check if the weakref became invalid
                if wr() is None:
                    raise InvalidateHandlerException()
                raise

        wr = weakref.ref(self)
        for event, fname in self.event_map.items():
            #
            # Do not trust the implicit scope and pass the
            # weakref explicitly via partial
            #
            (self
             .ndb
             .register_handler(event,
                               partial(wr_handler, wr, fname)))

    def snapshot(self, ctxid=None):
        '''
        Create and return a snapshot of the object. The method creates
        corresponding SQL tables for the object itself and for detected
        dependencies.

        The snapshot tables will be removed as soon as the snapshot gets
        collected by the GC.
        '''
        ctxid = ctxid or self.ctxid or id(self)
        if self._replace is None:
            key = self.key
        else:
            key = self._replace.key
        snp = type(self)(self.view, key, ctxid=ctxid)
        self.ndb.schema.save_deps(ctxid, weakref.ref(snp), self.iclass)
        snp.changed = set(self.changed)
        return snp

    def complete_key(self, key):
        '''
        Try to complete the object key based on the provided fields.
        E.g.::

            >>> ndb.interfaces['eth0'].complete_key({"ifname": "eth0"})
            {'ifname': 'eth0',
             'index': 2,
             'target': u'localhost',
             'tflags': 0}

        It is an internal method and is not supposed to be used externally.
        '''
        self.log.debug('complete key %s from table %s' % (key, self.etable))
        fetch = []
        if isinstance(key, Record):
            key = key._as_dict()
        for name in self.kspec:
            if name not in key:
                fetch.append('f_%s' % name)

        if fetch:
            keys = []
            values = []
            for name, value in key.items():
                nla_name = self.iclass.name2nla(name)
                if nla_name in self.spec:
                    name = nla_name
                if value is not None and name in self.spec:
                    keys.append('f_%s = %s' % (name, self.schema.plch))
                    values.append(value)
            spec = (self
                    .ndb
                    .schema
                    .fetchone('SELECT %s FROM %s WHERE %s' %
                              (' , '.join(fetch),
                               self.etable,
                               ' AND '.join(keys)),
                              values))
            if spec is None:
                self.log.debug('got none')
                return None
            for name, value in zip(fetch, spec):
                key[name[2:]] = value

        self.log.debug('got %s' % key)
        return key

    def rollback(self, snapshot=None):
        '''
        Try to rollback the object state using the snapshot provided as
        an argument or using `self.last_save`.
        '''
        if self._replace is not None:
            self.log.debug('rollback replace: %s :: %s'
                           % (self.key, self._replace.key))
            new_replace = type(self)(self.view, self.key)
            new_replace.state.set('remove')
            self.state.set('replace')
            self.update(self._replace)
            self._replace = new_replace
        self.log.debug('rollback: %s' % str(self.state.events))
        snapshot = snapshot or self.last_save
        snapshot.state.set(self.state.get())
        snapshot.apply(rollback=True)
        for link, snp in snapshot.snapshot_deps:
            link.rollback(snapshot=snp)
        return self

    def clear(self):
        pass

    @property
    def clean(self):
        return self.state == 'system' and \
            not self.changed and \
            not self._apply_script

    def commit(self):
        '''
        Try to commit the pending changes. If the commit fails,
        automatically revert the state.
        '''
        if self.clean:
            return self

        if self.chain:
            self.chain.commit()
        self.log.debug('commit: %s' % str(self.state.events))
        # Is it a new object?
        if self.state == 'invalid':
            # Save values, try to apply
            save = dict(self)
            try:
                return self.apply()
            except Exception as e_i:
                # Save the debug info
                e_i.trace = traceback.format_exc()
                # ACHTUNG! The routine doesn't clean up the system
                #
                # Drop all the values and rollback to the initial state
                for key in tuple(self.keys()):
                    del self[key]
                for key in save:
                    dict.__setitem__(self, key, save[key])
                raise e_i

        # Continue with an existing object

        # The snapshot tables in the DB will be dropped as soon as the GC
        # collects the object. But in the case of an exception the `snp`
        # variable will be saved in the traceback, so the tables will be
        # available to debug. If the traceback will be saved somewhere then
        # the tables will never be dropped by the GC, so you can do it
        # manually by `ndb.schema.purge_snapshots()` -- to invalidate all
        # the snapshots and to drop the associated tables.

        # Apply the changes
        try:
            self.apply()
        except Exception as e_c:
            # Rollback in the case of any error
            try:
                self.rollback()
            except Exception as e_r:
                e_c.chain = [e_r]
                if hasattr(e_r, 'chain'):
                    e_c.chain.extend(e_r.chain)
                e_r.chain = None
            raise
        finally:
            (self
             .last_save
             .state
             .set(self.state.get()))
        if self._replace is not None:
            self._replace = None
        return self

    def remove(self):
        with self.lock:
            self.state.set('remove')
            return self

    def check(self):
        state_map = (('invalid', 'system'),
                     ('remove', 'invalid'),
                     ('setns', 'invalid'),
                     ('setns', 'system'),
                     ('replace', 'system'))

        self.load_sql()
        self.log.debug('check: %s' % str(self.state.events))

        if self.state.transition() not in state_map:
            self.log.debug('check state: False')
            return False

        if self.changed:
            self.log.debug('check changed: %s' % (self.changed))
            return False

        self.log.debug('check: True')
        return True

    def make_req(self, prime):
        req = dict(prime)
        for key in self.changed:
            req[key] = self[key]
        return req

    def get_count(self):
        conditions = []
        values = []
        for name in self.kspec:
            conditions.append('f_%s = %s' % (name, self.schema.plch))
            values.append(self.get(self.iclass.nla2name(name), None))
        return (self
                .ndb
                .schema
                .fetchone('''
                          SELECT count(*) FROM %s WHERE %s
                          ''' % (self.table,
                                 ' AND '.join(conditions)),
                          values))[0]

    def hook_apply(self, method, **spec):
        pass

    def apply(self, rollback=False):
        '''
        Create a snapshot and apply pending changes. Do not revert
        the changes in the case of an exception.
        '''

        # Save the context
        if not rollback and self.state != 'invalid':
            self.last_save = self.snapshot()

        self.log.debug('apply: %s' % str(self.state.events))
        self.load_event.clear()
        self._apply_script_snapshots = []

        # Load the current state
        try:
            self.schema.commit()
        except:
            pass
        self.load_sql(set_state=False)
        if self.state == 'system' and self.get_count() == 0:
            state = self.state.set('invalid')
        else:
            state = self.state.get()

        # Create the request.
        idx_req = dict([(x, self[self.iclass.nla2name(x)]) for x
                        in self.schema.compiled[self.table]['idx']
                        if self.iclass.nla2name(x) in self])
        req = self.make_req(idx_req)
        self.log.debug('apply req: %s' % str(req))
        self.log.debug('apply idx_req: %s' % str(idx_req))
        self.log.debug('apply state: %s' % state)

        method = None
        ignore = tuple()
        #
        if state in ('invalid', 'replace'):
            req = dict([x for x in self.items() if x[1] is not None])
            for l_key, r_key in self.match_pairs.items():
                for src in self.match_src:
                    try:
                        req[l_key] = src[r_key]
                        break
                    except:
                        pass
            method = 'add'
            ignore = {errno.EEXIST: 'set'}
        elif state == 'system':
            method = 'set'
        elif state == 'setns':
            method = 'set'
            ignore = {errno.ENODEV: None}
        elif state == 'remove':
            method = 'del'
            req = idx_req
            ignore = {errno.ENODEV: None,         # interfaces
                      errno.ESRCH: None,          # routes
                      errno.EADDRNOTAVAIL: None}  # addresses
        else:
            raise Exception('state transition not supported')

        for itn in range(20):
            try:
                self.log.debug('run %s (%s)' % (method, req))
                (self
                 .sources[self['target']]
                 .api(self.api, method, **req))
                (self
                 .hook_apply(method, **req))
            except NetlinkError as e:
                (self
                 .log
                 .debug('error: %s' % e))
                if e.code in ignore:
                    self.log.debug('ignore error %s for %s' % (e.code, self))
                    if ignore[e.code] is not None:
                        self.log.debug('run fallback %s (%s)'
                                       % (ignore[e.code], req))
                        try:
                            (self
                             .sources[self['target']]
                             .api(self.api, ignore[e.code], **req))
                        except NetlinkError:
                            pass
                else:
                    raise e

            wtime = self.wtime(itn)
            mqsize = self.view.ndb._event_queue.qsize()
            nq = self.schema.stats.get(self['target'])
            if nq is not None:
                nqsize = nq.qsize
            else:
                nqsize = 0
            self.log.debug('stats: apply %s {'
                           'objid %s, wtime %s, '
                           'mqsize %s, nqsize %s'
                           '}' % (method, id(self), wtime, mqsize, nqsize))
            if self.check():
                self.log.debug('checked')
                break
            self.log.debug('check failed')
            self.load_event.wait(wtime)
            self.load_event.clear()
        else:
            self.log.debug('stats: %s apply %s fail' % (id(self), method))
            raise Exception('lost sync in apply()')

        self.log.debug('stats: %s pass' % (id(self)))
        #
        if state == 'replace':
            self._replace.remove()
            self._replace.apply()
        #
        if rollback:
            #
            # Iterate all the snapshot tables and collect the diff
            for (table, cls) in self.view.classes.items():
                if issubclass(type(self), cls) or \
                        issubclass(cls, type(self)):
                    continue
                # comprare the tables
                diff = (self
                        .ndb
                        .schema
                        .fetch('''
                               SELECT * FROM %s_%s
                                 EXCEPT
                               SELECT * FROM %s
                               '''
                               % (table, self.ctxid, table)))
                for record in diff:
                    record = dict(zip((self
                                       .schema
                                       .compiled[table]['all_names']),
                                      record))
                    key = dict([x for x in record.items()
                                if x[0] in self.schema.compiled[table]['idx']])
                    obj = self.view.get(key, table)
                    obj.load_sql(ctxid=self.ctxid)
                    obj.state.set('invalid')
                    try:
                        obj.apply()
                    except Exception as e:
                        self.errors.append((time.time(), obj, e))
        else:
            for op, argv, kwarg in self._apply_script:
                ret = op(*argv, **kwarg)
                if isinstance(ret, Exception):
                    raise ret
                elif ret is not None:
                    self._apply_script_snapshots.append(ret)
            self._apply_script = []
        return self

    def update(self, data):
        for key, value in data.items():
            self.load_value(key, value)

    def load_value(self, key, value):
        '''
        Load a value and clean up the `self.changed` set if the
        loaded value matches the expectation.
        '''
        if key not in self.changed:
            dict.__setitem__(self, key, value)
        elif self.get(key) == value:
            self.changed.remove(key)
        elif key in self.fields_cmp and self.fields_cmp[key](self, value):
            self.changed.remove(key)

    def load_sql(self, table=None, ctxid=None, set_state=True):
        '''
        Load the data from the database.
        '''

        if not self.key:
            return

        if table is None:
            if ctxid is None:
                table = self.etable
            else:
                table = '%s_%s' % (self.table, ctxid)
        keys = []
        values = []

        for name, value in self.key.items():
            keys.append('f_%s = %s' % (name, self.schema.plch))
            values.append(value)

        spec = (self
                .ndb
                .schema
                .fetchone('SELECT * FROM %s WHERE %s' %
                          (table, ' AND '.join(keys)), values))
        self.log.debug('load_sql: %s' % str(spec))
        if set_state:
            with self.lock:
                if spec is None:
                    if self.state != 'invalid':
                        # No such object (anymore)
                        self.state.set('invalid')
                        self.changed = set()
                elif self.state not in ('remove', 'setns'):
                    self.update(dict(zip(self.names, spec)))
                    self.state.set('system')
        return spec

    def load_rtnlmsg(self, target, event):
        '''
        Check if the RTNL event matches the object and load the
        data from the database if it does.
        '''
        # TODO: partial match (object rename / restore)
        # ...

        # full match
        for name in self.knorm:
            value = self.get(name)
            if name == 'target':
                if value != target:
                    return
            elif name == 'tflags':
                continue
            elif value != (event.get_attr(name) or event.get(name)):
                return

        self.log.debug('load_rtnl: %s' % str(event.get('header')))
        if event['header'].get('type', 0) % 2:
            self.state.set('invalid')
            self.changed = set()
        else:
            self.load_sql()
        self.load_event.set()
