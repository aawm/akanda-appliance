# Copyright 2014 DreamHost, LLC
#
# Author: DreamHost, LLC
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.


import re

import netaddr

from akanda.router import models
from akanda.router.drivers import base


GENERIC_IFNAME = 'ge'
PHYSICAL_INTERFACES = ['lo', 'eth', 'em', 're', 'en', 'vio', 'vtnet']
ULA_PREFIX = 'fdca:3ba5:a17a:acda::/64'


class InterfaceManager(base.Manager):
    """
    """
    EXECUTABLE = '/sbin/ifconfig'

    def __init__(self, root_helper='sudo'):
        super(InterfaceManager, self).__init__(root_helper)
        self.next_generic_index = 0
        self.host_mapping = {}
        self.generic_mapping = {}

    def ensure_mapping(self):
        if not self.host_mapping:
            self.get_interfaces()

    def get_interfaces(self):
        interfaces = _parse_interfaces(self.do('-a'),
                                       filters=PHYSICAL_INTERFACES)

        interfaces.sort(key=lambda x: x.ifname)
        for i in interfaces:
            if i.ifname not in self.host_mapping:
                generic_name = 'ge%d' % self.next_generic_index
                self.host_mapping[i.ifname] = generic_name
                self.next_generic_index += 1

            # change ifname to generic version
            i.ifname = self.host_mapping[i.ifname]
        self.generic_mapping = dict((v, k) for k, v in
                                    self.host_mapping.iteritems())

        return interfaces

    def get_interface(self, ifname):
        real_ifname = self.generic_to_host(ifname)
        retval = _parse_interface(self.do(real_ifname))
        retval.ifname = ifname
        return retval

    def is_valid(self, ifname):
        self.ensure_mapping()
        return ifname in self.generic_mapping

    def generic_to_host(self, generic_name):
        self.ensure_mapping()
        return self.generic_mapping.get(generic_name)

    def host_to_generic(self, real_name):
        self.ensure_mapping()
        return self.host_mapping.get(real_name)

    def update_interfaces(self, interfaces):
        for i in interfaces:
            self.update_interface(i)

    def up(self, interface):
        real_ifname = self.generic_to_host(interface.ifname)
        self.sudo(real_ifname, 'up')
        return self.get_interface(interface.ifname)

    def down(self, interface):
        real_ifname = self.generic_to_host(interface.ifname)
        self.sudo(real_ifname, 'down')

    def update_interface(self, interface, ignore_link_local=True):
        real_ifname = self.generic_to_host(interface.ifname)
        old_interface = self.get_interface(interface.ifname)

        if ignore_link_local:
            interface.addresses = [a for a in interface.addresses
                                   if not a.is_link_local()]
            old_interface.addresses = [a for a in old_interface.addresses
                                       if not a.is_link_local()]
        self._update_description(real_ifname, interface)
        # Must update primary before aliases otherwise will lose address
        # in case where primary and alias are swapped.
        self._update_addresses(real_ifname, interface, old_interface)

    def _update_description(self, real_ifname, interface):
        if interface.description:
            self.sudo(real_ifname, 'description', interface.description)

    def _update_addresses(self, real_ifname, interface, old_interface):
        family = {4: 'inet', 6: 'inet6'}

        add = lambda a: (
            real_ifname, family[a[0].version], 'add', '%s/%s' % (a[0], a[1])
        )
        delete = lambda a: (
            real_ifname, family[a[0].version], 'del', '%s/%s' % (a[0], a[1])
        )
        mutator = lambda a: (a.ip, a.prefixlen)

        self._update_set(real_ifname, interface, old_interface,
                         'all_addresses', add, delete, mutator)

    def _update_set(self, real_ifname, interface, old_interface, attribute,
                    fmt_args_add, fmt_args_delete, mutator=lambda x: x):

        next_set = set(mutator(i) for i in getattr(interface, attribute))
        prev_set = set(mutator(i) for i in getattr(old_interface, attribute))

        if next_set == prev_set:
            return

        for item in (next_set - prev_set):
            self.sudo(*fmt_args_add(item))

        for item in (prev_set - next_set):
            self.sudo(*fmt_args_delete(item))

    def get_management_address(self, ensure_configuration=False):
        primary = self.get_interface(GENERIC_IFNAME + '0')
        prefix, prefix_len = ULA_PREFIX.split('/', 1)
        eui = netaddr.EUI(primary.lladdr)
        ip_str = str(eui.ipv6_link_local()).replace('fe80::', prefix[:-1])

        if not primary.is_up:
            self.up(primary)

        ip = netaddr.IPNetwork('%s/%s' % (ip_str, prefix_len))
        if ensure_configuration and ip not in primary.addresses:
            primary.addresses.append(ip)
            self.update_interface(primary)
        return ip_str


def get_rug_address():
    """ Return the RUG address """
    net = netaddr.IPNetwork(ULA_PREFIX)
    return str(netaddr.IPAddress(net.first + 1))


def _parse_interfaces(data, filters=None):
    retval = []
    for iface_data in re.split('(^|\n)(?=\w+\d{0,3}\s+Link)', data, re.M):
        if not iface_data.strip():
            continue

        # FIXME (mark): the logic works, but should be more readable
        for f in filters or ['']:
            if f == '':
                break
            elif iface_data.startswith(f) and iface_data[len(f)].isdigit():
                break
        else:
            continue

        retval.append(_parse_interface(iface_data))
    return retval


def _parse_interface(data):
    retval = dict(addresses=[])
    for line in data.split('\n'):
        if line.startswith(' '):
            line = line.strip()
            if line.startswith('inet'):
                retval['addresses'].append(_parse_inet(line))
            elif 'MTU' in line:
                retval.update(_parse_mtu_and_flags(line))
        else:
            retval.update(_parse_head(line))

    return models.Interface.from_dict(retval)


def _parse_head(line):
    retval = {}
    m = re.match('(?P<ifname>\w+\d{1,3})', line)
    if m:
        retval['ifname'] = m.group('ifname')
        retval['lladdr'] = line.split('HWaddr')[1].strip()
    return retval


def _parse_mtu_and_flags(line):
    retval = {}
    parts = line.split()
    for part in parts:
        if part.startswith('MTU:'):
            retval['mtu'] = int(part.split(':')[1])
        elif part.startswith('Metric:'):
            retval['metric'] = int(part.split(':')[1])
        else:
            retval.setdefault('flags', []).append(part)
    return retval


def _parse_inet(line):
    tokens = line.split()
    if tokens[0] == 'inet6':
        return netaddr.IPNetwork(tokens[2])

    ip = re.search('addr:(?P<addr>[0-9\.]+)', line).group('addr')
    mask = re.search('Mask:(?P<mask>[0-9\.]+)', line).group('mask')
    return netaddr.IPNetwork('%s/%s' % (ip, mask))
