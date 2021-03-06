from pritunl.constants import *
from pritunl.exceptions import *
from pritunl.helpers import *
from pritunl import utils
from pritunl import mongo
from pritunl import ipaddress
from pritunl import settings

import hashlib
import json
import datetime
import pymongo
import collections

class Host(mongo.MongoObject):
    fields = {
        'name',
        'link_id',
        'location_id',
        'secret',
        'status',
        'active',
        'timeout',
        'priority',
        'ping_timestamp_ttl',
        'static',
        'public_address',
        'local_address',
        'address6',
        'version',
    }
    fields_default = {
        'status': UNAVAILABLE,
        'active': False,
        'static': False,
    }

    def __init__(self, link=None, location=None, name=None, link_id=None,
            location_id=None, secret=None, status=None, active=None,
            timeout=None, priority=None, ping_timestamp_ttl=None,
            static=None, public_address=None, local_address=None,
            address6=None, version=None, tunnels=None, **kwargs):
        mongo.MongoObject.__init__(self, **kwargs)

        self.link = link
        self.location = location

        if name is not None:
            self.name = name

        if link_id is not None:
            self.link_id = link_id

        if location_id is not None:
            self.location_id = location_id

        if secret is not None:
            self.secret = secret

        if status is not None:
            self.status = status

        if active is not None:
            self.active = active

        if timeout is not None:
            self.timeout = timeout

        if priority is not None:
            self.priority = priority

        if ping_timestamp_ttl is not None:
            self.ping_timestamp_ttl = ping_timestamp_ttl

        if static is not None:
            self.static = static

        if public_address is not None:
            self.public_address = public_address

        if local_address is not None:
            self.local_address = local_address

        if address6 is not None:
            self.address6 = address6

        if version is not None:
            self.version = version

        if tunnels is not None:
            self.tunnels = tunnels

    @cached_static_property
    def collection(cls):
        return mongo.get_collection('links_hosts')

    @property
    def is_available(self):
        if self.status != AVAILABLE or \
                not self.ping_timestamp_ttl or \
                utils.now() > self.ping_timestamp_ttl:
            return False
        return True

    def dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'link_id': self.link_id,
            'location_id': self.location_id,
            'status': ACTIVE if self.active and \
                self.link.status == ONLINE else self.status,
            'timeout': self.timeout,
            'priority': self.priority,
            'ping_timestamp_ttl': self.ping_timestamp_ttl,
            'static': bool(self.static),
            'public_address': self.public_address if not \
                settings.app.demo_mode else utils.random_ip_addr(),
            'local_address': self.local_address if not \
                settings.app.demo_mode else utils.random_ip_addr(),
            'address6': self.address6 if not \
                settings.app.demo_mode else None,
            'version': self.version,
        }

    def simple_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'link_id': self.link_id,
            'location_id': self.location_id,
            'timeout': self.timeout,
            'priority': self.priority,
            'ping_timestamp_ttl': self.ping_timestamp_ttl,
            'static': bool(self.static),
            'public_address': self.public_address if not \
                settings.app.demo_mode else utils.random_ip_addr(),
            'local_address': self.local_address if not \
                settings.app.demo_mode else utils.random_ip_addr(),
            'address6': self.address6 if not \
                settings.app.demo_mode else None,
            'version': self.version,
            'uri': self.get_uri(),
        }

    def check_available(self):
        if self.is_available:
            return True

        response = self.collection.update({
            '_id': self.id,
            'ping_timestamp_ttl': self.ping_timestamp_ttl,
        }, {'$set': {
            'status': UNAVAILABLE,
            'active': False,
            'ping_timestamp_ttl': None,
        }})

        if response['updatedExisting']:
            self.active = False
            self.status = UNAVAILABLE
            self.ping_timestamp_ttl = None
            return False

        return True

    def load_link(self):
        self.link = Link(id=self.link_id)
        self.location = Location(link=self.link, id=self.location_id)

    def generate_secret(self):
        self.secret = utils.rand_str(32)

    def get_uri(self):
        return 'pritunl://%s:%s@' % (self.id, self.secret)

    def set_active(self):
        response = self.collection.update({
            '_id': self.id,
            'status': AVAILABLE,
        }, {'$set': {
            'active': True,
        }})
        if not response['updatedExisting']:
            return False

        Host.collection.update_many({
            '_id': {'$ne': self.id},
            'location_id': self.location_id,
        }, {'$set': {
            'active': False,
        }})

        return True

    def set_inactive(self):
        self.status = UNAVAILABLE
        self.active = False
        self.ping_timestamp_ttl = None
        self.commit(('status', 'active', 'ping_timestamp_ttl'))

    def get_state_locations(self):
        loc_excludes = set()
        for exclude in self.link.excludes:
            if self.location.id not in exclude:
                continue

            if exclude[0] == self.location.id:
                exclude_id = exclude[1]
            else:
                exclude_id = exclude[0]

            loc_excludes.add(exclude_id)

        locations = []
        locations_id = {}

        for location in self.link.iter_locations():
            locations.append(location)
            locations_id[location.id] = location

        return locations, locations_id, loc_excludes

    def get_state(self):
        self.status = AVAILABLE
        self.ping_timestamp_ttl = utils.now() + datetime.timedelta(
            seconds=self.timeout or settings.vpn.link_timeout)
        self.commit(('public_address', 'address6', 'local_address', 'version',
            'status', 'ping_timestamp_ttl'))

        if not self.link.key:
            self.link.generate_key()
            self.link.commit('key')
            return

        links = []
        state = {
            'id': self.id,
            'ipv6': self.link.ipv6,
            'type': self.location.type,
            'links': links,
        }
        active_host = self.location.get_active_host()
        active = active_host and active_host.id == self.id

        loc_transit_excludes = set(self.location.transit_excludes)
        locations, locations_id, loc_excludes = self.get_state_locations()

        if self.link.status == ONLINE and active_host and active:
            if self.link.type == DIRECT:
                other_location = None

                for location in locations:
                    if location.id == self.location.id:
                        continue

                    if location.type != self.location.type:
                        other_location = location

                active_host = other_location.get_active_host()
                if active_host:
                    if self.location.type == DIRECT_SERVER:
                        left_subnets = ['%s/32' % self.local_address]
                        right_subnets = ['%s/32' % active_host.local_address]
                    else:
                        left_subnets = ['%s/32' % self.local_address]
                        right_subnets = ['%s/32' % active_host.local_address]

                    links.append({
                        'pre_shared_key': self.link.key,
                        'right': active_host.address6 \
                            if self.link.ipv6 else active_host.public_address,
                        'left_subnets': left_subnets,
                        'right_subnets': right_subnets,
                    })
            else:
                for location in locations:
                    if location.id in loc_excludes or \
                            location.id == self.location.id:
                        continue

                    active_host = location.get_active_host()
                    if not active_host:
                        continue

                    excludes = set()
                    transit_excludes = set(self.location.transit_excludes)
                    for exclude in self.link.excludes:
                        if location.id not in exclude:
                            continue

                        if exclude[0] == location.id:
                            exclude_id = exclude[1]
                        else:
                            exclude_id = exclude[0]

                        excludes.add(exclude_id)

                    left_subnets = []
                    for route in self.location.routes.values():
                        if route['network'] not in left_subnets:
                            left_subnets.append(route['network'])

                    for transit_id in self.location.transits:
                        if transit_id != self.id and \
                                transit_id in excludes and \
                                transit_id in locations_id and \
                                transit_id not in loc_transit_excludes:
                            transit_loc = locations_id[transit_id]
                            for route in transit_loc.routes.values():
                                if route['network'] not in left_subnets:
                                    left_subnets.append(route['network'])

                    right_subnets = []
                    for route in location.routes.values():
                        right_subnets.append(route['network'])

                    for transit_id in location.transits:
                        if transit_id != self.id and \
                                transit_id in loc_excludes and \
                                transit_id in locations_id and \
                                transit_id not in transit_excludes:
                            transit_loc = locations_id[transit_id]
                            for route in transit_loc.routes.values():
                                if route['network'] not in left_subnets:
                                    right_subnets.append(route['network'])

                    links.append({
                        'pre_shared_key': self.link.key,
                        'right': active_host.address6 \
                            if self.link.ipv6 else active_host.public_address,
                        'left_subnets': left_subnets,
                        'right_subnets': right_subnets,
                    })

        state['hash'] = hashlib.md5(json.dumps(
            state,
            sort_keys=True,
            default=lambda x: str(x),
        )).hexdigest()

        return state, active

    def get_static_links(self):
        if not self.link.key:
            self.link.generate_key()
            self.link.commit('key')
            return

        links = []

        loc_transit_excludes = set(self.location.transit_excludes)
        locations, locations_id, loc_excludes = self.get_state_locations()

        if self.link.type == DIRECT:
            other_location = None

            for location in locations:
                if location.id == self.location.id:
                    continue

                if location.type != self.location.type:
                    other_location = location

            active_host = other_location.get_active_host()
            if not active_host:
                for host in other_location.iter_hosts():
                    active_host = host
                    break

            if active_host:
                if self.location.type == DIRECT_SERVER:
                    left_subnets = ['%s/32' % self.local_address]
                    right_subnets = ['%s/32' % active_host.local_address]
                else:
                    left_subnets = ['%s/32' % self.local_address]
                    right_subnets = ['%s/32' % active_host.local_address]

                links.append({
                    'pre_shared_key': self.link.key,
                    'right': active_host.address6 \
                        if self.link.ipv6 else active_host.public_address,
                    'left_subnets': left_subnets,
                    'right_subnets': right_subnets,
                })
        else:
            for location in locations:
                if location.id in loc_excludes or \
                        location.id == self.location.id:
                    continue

                active_host = location.get_active_host()
                if not active_host:
                    for host in location.iter_hosts():
                        active_host = host
                        break

                if not active_host:
                    continue

                excludes = set()
                transit_excludes = set(self.location.transit_excludes)
                for exclude in self.link.excludes:
                    if location.id not in exclude:
                        continue

                    if exclude[0] == location.id:
                        exclude_id = exclude[1]
                    else:
                        exclude_id = exclude[0]

                    excludes.add(exclude_id)

                left_subnets = []
                for route in self.location.routes.values():
                    if route['network'] not in left_subnets:
                        left_subnets.append(route['network'])

                for transit_id in self.location.transits:
                    if transit_id != self.id and \
                            transit_id in excludes and \
                            transit_id in locations_id and \
                            transit_id not in loc_transit_excludes:
                        transit_loc = locations_id[transit_id]
                        for route in transit_loc.routes.values():
                            if route['network'] not in left_subnets:
                                left_subnets.append(route['network'])

                right_subnets = []
                for route in location.routes.values():
                    right_subnets.append(route['network'])

                for transit_id in location.transits:
                    if transit_id != self.id and \
                            transit_id in loc_excludes and \
                            transit_id in locations_id and \
                            transit_id not in transit_excludes:
                        transit_loc = locations_id[transit_id]
                        for route in transit_loc.routes.values():
                            if route['network'] not in left_subnets:
                                right_subnets.append(route['network'])

                links.append({
                    'pre_shared_key': self.link.key,
                    'right': active_host.address6 \
                        if self.link.ipv6 else active_host.public_address,
                    'left_subnets': left_subnets,
                    'right_subnets': right_subnets,
                })

        return links

    def get_static_conf(self):
        secrets = ''
        conns = ''

        for i, lnk in enumerate(self.get_static_links()):
            secrets += IPSEC_SECRET % (
                self.public_address,
                lnk['right'],
                lnk['pre_shared_key'],
            ) + '\n'

            conns += IPSEC_CONN % (
                '%s-%d' % (self.id, i),
                self.public_address,
                ','.join(lnk['left_subnets']),
                lnk['right'],
                lnk['right'],
                ','.join(lnk['right_subnets']),
            )

        return secrets + '\n' + conns.rstrip()

    def get_ubnt_conf(self):
        conf = UBNT_CONF

        for i, lnk in enumerate(self.get_static_links()):
            if not len(lnk['left_subnets']) or not len(lnk['right_subnets']):
                continue

            conf += UBNT_PEER % (
                lnk['right'],
                lnk['right'],
                lnk['pre_shared_key'],
                lnk['right'],
                lnk['right'],
                self.public_address,
                lnk['right'],
                lnk['right'],
                lnk['right'],
                lnk['left_subnets'][0],
                lnk['right'],
                lnk['right_subnets'][0],
            )

        return conf.rstrip()

class Location(mongo.MongoObject):
    fields = {
        'name',
        'type',
        'link_id',
        'routes',
        'status',
        'transits',
        'transit_excludes',
    }
    fields_default = {
        'routes': {},
        'transits': [],
        'transit_excludes': [],
    }

    def __init__(self, link=None, name=None, type=None, link_id=None,
            routes=None, **kwargs):
        mongo.MongoObject.__init__(self, **kwargs)

        self.link = link

        if name is not None:
            self.name = name

        if type is not None:
            self.type = type

        if link_id is not None:
            self.link_id = link_id

        if routes is not None:
            self.routes = routes

    @cached_static_property
    def collection(cls):
        return mongo.get_collection('links_locations')

    @cached_static_property
    def host_collection(cls):
        return mongo.get_collection('links_hosts')

    def dict(self, locations=None, locations_id=None):
        static_location = False

        location_online = False
        hosts = []
        for hst in self.iter_hosts():
            if hst.active:
                location_online = True

            hosts.append(hst.dict())
            if hst.static:
                static_location = True

        routes = []
        for route_id, route in self.routes.items():
            route['id'] = route_id
            route['link_id'] = self.link_id
            route['location_id'] = self.id
            routes.append(route)

        status = self.status or {}
        if self.link.status != ONLINE or not location_online:
            status = {}

        peers = []
        if locations:
            peers_name = collections.defaultdict(list)
            peers_names = set()

            excludes = set()
            for exclude in self.link.excludes:
                if self.id not in exclude:
                    continue

                if exclude[0] == self.id:
                    exclude_id = exclude[1]
                else:
                    exclude_id = exclude[0]

                excludes.add(exclude_id)

            transit_excludes = set(self.transit_excludes)
            transited_ids = set()
            transited_locations = []

            i = 0
            for location in locations:
                if location.id in excludes or location.id == self.id:
                    continue

                for transit_id in location.transits:
                    if transit_id != self.id and \
                            transit_id in excludes and \
                            transit_id in locations_id and \
                            transit_id not in transited_ids and \
                            transit_id not in transit_excludes:
                        transited_ids.add(transit_id)

                        transited_locations.append({
                            'id': transit_id,
                            'name': locations_id[transit_id].name,
                            'transit': transit_id in self.transits,
                            'transited_id': location.id,
                            'transited_name': location.name,
                            'status': status.get(str(i)) or 'disconnected',
                            'static': static_location,
                        })

                peers_names.add(location.name)
                peers_name[location.name].append({
                    'id': location.id,
                    'name': location.name,
                    'transit': location.id in self.transits,
                    'transited_id': None,
                    'transited_name': None,
                    'status': status.get(str(i)) or 'disconnected',
                    'static': static_location,
                })

                i += 1

            for name in sorted(list(peers_names)):
                for peer in peers_name[name]:
                    peers.append(peer)

            peers += transited_locations

        return {
            'id': self.id,
            'name': self.name,
            'type': self.type,
            'ipv6': self.link.ipv6,
            'link_id': self.link_id,
            'link_type': self.link.type,
            'hosts': hosts,
            'routes': routes,
            'peers': peers,
        }

    def remove(self):
        Host.collection.remove({
            'location_id': self.id,
        })
        mongo.MongoObject.remove(self)

    def add_route(self, network):
        try:
            network = str(ipaddress.IPNetwork(network))
        except ValueError:
            raise NetworkInvalid('Network address is invalid')

        network_id = hashlib.md5(network).hexdigest()
        self.routes[network_id] = {
            'network': network,
        }

        return network_id

    def remove_route(self, network_id):
        self.routes.pop(network_id)

    def add_exclude(self, exclude_id):
        exclude = [self.id, exclude_id]
        exclude.sort(key=lambda x: str(x))

        if exclude in self.link.excludes:
            if exclude_id not in self.transit_excludes:
                self.transit_excludes.append(exclude_id)
        else:
            self.link.excludes.append(exclude)

        self.link.excludes.sort()
        self.transit_excludes.sort()

    def remove_exclude(self, exclude_id):
        exclude = [self.id, exclude_id]
        exclude.sort(key=lambda x: str(x))

        try:
            self.link.excludes.remove(exclude)
        except ValueError:
            pass

        try:
            self.transit_excludes.remove(exclude_id)
        except ValueError:
            pass

    def add_transit(self, transit_id):
        if transit_id not in self.transits:
            self.transits.append(transit_id)
        self.transits.sort()

    def remove_transit(self, transit_id):
        try:
            self.transits.remove(transit_id)
        except ValueError:
            pass

    def get_host(self, host_id):
        return Host(link=self.link, location=self, id=host_id)

    def iter_hosts(self):
        cursor = Host.collection.find({
            'location_id': self.id,
        }).sort('name')

        for doc in cursor:
            yield Host(link=self.link, location=self, doc=doc)

    def get_static_host(self):
        if self.link.status != ONLINE:
            return

        doc = Host.collection.find_one({
            'location_id': self.id,
            'static': True,
        })

        if doc:
            return Host(link=self.link, location=self, doc=doc)

    def get_active_host(self):
        if self.link.status != ONLINE:
            return

        hst = self.get_static_host()
        if hst:
            return hst

        doc = Host.collection.find_one({
            'location_id': self.id,
            'status': AVAILABLE,
            'active': True,
        })

        if doc:
            return Host(link=self.link, location=self, doc=doc)

        cursor = Host.collection.find({
            'location_id': self.id,
            'status': AVAILABLE,
            'active': False,
        }, {
            '_id': True,
        }).sort('priority', pymongo.DESCENDING).limit(1)

        doc = None
        for doc in cursor:
            break

        if not doc:
            return

        doc_id = doc['_id']
        response = Host.collection.update({
            '_id': doc_id,
            'status': AVAILABLE,
            'active': False,
        }, {'$set': {
            'active': True,
        }})
        if not response['updatedExisting']:
            return

        Host.collection.update_many({
            '_id': {'$ne': doc_id},
            'location_id': self.id,
        }, {'$set': {
            'active': False,
        }})

        return Host(link=self.link, location=self, doc=doc)


class Link(mongo.MongoObject):
    fields = {
        'name',
        'type',
        'status',
        'key',
        'excludes',
        'ipv6',
    }
    fields_default = {
        'type': SITE_TO_SITE,
        'status': OFFLINE,
        'excludes': [],
    }

    def __init__(self, name=None, type=None, status=None, timeout=None,
            key=None, ipv6=None, **kwargs):
        mongo.MongoObject.__init__(self, **kwargs)

        if name is not None:
            self.name = name

        if type is not None:
            self.type = type

        if status is not None:
            self.status = status

        if timeout is not None:
            self.timeout = timeout

        if key is not None:
            self.key = key

        if ipv6 is not None:
            self.ipv6 = ipv6

    @cached_static_property
    def collection(cls):
        return mongo.get_collection('links')

    def dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'type': self.type,
            'ipv6': self.ipv6,
            'status': self.status,
        }

    def remove(self):
        Host.collection.remove({
            'link_id': self.id,
        })
        Location.collection.remove({
            'link_id': self.id,
        })
        mongo.MongoObject.remove(self)

    def generate_key(self):
        self.key = utils.rand_str(64)

    def get_location(self, location_id):
        return Location(link=self, id=location_id)

    def iter_locations(self, skip=None, exclude_id=None):
        if exclude_id:
            excludes = self.excludes

        cursor = Location.collection.find({
            'link_id': self.id,
        }).sort('_id')

        for doc in cursor:
            if skip and doc['_id'] == skip:
                continue

            if exclude_id:
                exclude = [exclude_id, doc['_id']]
                exclude.sort(key=lambda x: str(x))

                if exclude in excludes:
                    continue

            yield Location(link=self, doc=doc)

    def iter_locations_dict(self):
        cursor = Location.collection.find({
            'link_id': self.id,
        }).sort('_id')

        locations = []
        locations_id = {}
        locations_name = collections.defaultdict(list)
        locations_names = set()

        for doc in cursor:
            location = Location(link=self, doc=doc)
            locations.append(location)
            locations_id[location.id] = location

        for location in locations:
            locations_names.add(location.name)
            locations_name[location.name].append(location.dict(
                locations, locations_id))

        for name in sorted(list(locations_names)):
            for location in locations_name[name]:
                yield location
