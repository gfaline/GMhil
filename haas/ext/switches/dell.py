# Copyright 2013-2015 Massachusetts Open Cloud Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the
# License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS
# IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
# express or implied.  See the License for the specific language
# governing permissions and limitations under the License.
"""A switch driver for the Dell Powerconnect 5500 series.

Currently the driver uses telnet to connect to the switch's console; in
the long term we want to be using SNMP.
"""

import pexpect
import re
import logging
import schema
from sqlalchemy import Column, ForeignKey, Integer, String

from haas.model import Switch
from haas.ext.switches import _console

logger = logging.getLogger(__name__)


class PowerConnect55xx(Switch):
    api_name = 'http://schema.massopencloud.org/haas/switches/powerconnect55xx'

    __mapper_args__ = {
        'polymorphic_identity': api_name,
    }

    id = Column(Integer, ForeignKey('switch.id'), primary_key=True)
    hostname = Column(String, nullable=False)
    username = Column(String, nullable=False)
    password = Column(String, nullable=False)

    @staticmethod
    def validate(kwargs):
        schema.Schema({
            'username': basestring,
            'hostname': basestring,
            'password': basestring,
        }).validate(kwargs)

    def session(self):
        return _Session.connect(self)


class _Session(_console.Session):

    def __init__(self, config_prompt, if_prompt, main_prompt, switch, console):
        self.config_prompt = config_prompt
        self.if_prompt     = if_prompt
        self.main_prompt   = main_prompt
        self.switch        = switch
        self.console       = console

    def _sendline(self, line):
        logger.debug('Sending to switch %r: %r',
                      self.switch, line)
        self.console.sendline(line)

    @staticmethod
    def connect(switch):
        # connect to the switch, and log in:
        console = pexpect.spawn('telnet ' + switch.hostname)
        console.expect('User Name:')
        console.sendline(switch.username)
        console.expect('Password:')
        console.sendline(switch.password)

        logger.debug('Logged in to switch %r', switch)

        #Regex to handle different prompt at switch
        #[\r\n]+ will handle any newline
        #.+ will handle any character after newline
        # this sequence terminates with #
        console.expect(r'[\r\n]+.+#')
        cmd_prompt = console.after
        cmd_prompt = cmd_prompt.strip(' \r\n\t')

        #:-1 omits the last hash character
        config_prompt = re.escape(cmd_prompt[:-1] + '(config)#')
        if_prompt = re.escape(cmd_prompt[:-1] + '(config-if)#')
        main_prompt = re.escape(cmd_prompt)

        return _Session(config_prompt=config_prompt,
                        if_prompt=if_prompt,
                        main_prompt=main_prompt,
                        switch=switch,
                        console=console)

    def enter_if_prompt(self, interface):
        self._sendline('config')
        self._sendline('int ' + interface)

    def exit_if_prompt(self):
        self._sendline('exit')
        self._sendline('exit')

    def enable_vlan(self, vlan_id):
        self._sendline('sw mode trunk')
        self._sendline('sw trunk allowed vlan add ' + vlan_id)

    def disable_vlan(self, vlan_id):
        self._sendline('sw trunk allowed vlan remove ' + vlan_id)

    def set_native(self, old, new):
        if old is not None:
            self.disable_vlan(old)
        self._sendline('sw trunk native vlan ' + new)
        self.enable_vlan(new)

    def disable_native(self, vlan_id):
        self.disable_vlan(vlan_id)
        self._sendline('sw trunk native vlan none')

    def disconnect(self):
        self._sendline('exit')
        self.console.expect(pexpect.EOF)
        logger.debug('Logged out of switch %r', self.switch)

    def get_port_networks(self, ports):
        num_re = re.compile(r'(\d+)')
        port_configs = self._port_configs(ports)
        result = {}
        for k, v in port_configs.iteritems():
            native = v['Trunking Native Mode VLAN'].strip()
            match = re.match(num_re, native)
            if match:
                # We need to call groups to get the part of the string that
                # actually matched, because it could include some junk on the end,
                # e.g. "100 (Inactive)".
                num_str = match.groups()[0]
                native = int(num_str)
            else:
                native = None
            networks = []
            range_str = v['Trunking VLANs Enabled']
            for range_str in v['Trunking VLANs Enabled'].split(','):
                for num_str in range_str.split('-'):
                    num_str = num_str.strip()
                    match = re.match(num_re, num_str)
                    if match:
                        # There may be other tokens in the output, e.g.
                        # the string "(Inactive)" somteimtes appears.
                        # We should only use the value if it's an actual number.
                        num_str = match.groups()[0]
                        networks.append(('vlan/%s' % num_str, int(num_str)))
            if native is not None:
                networks.append(('vlan/native', native))
            result[k] = networks
        return result


    def _port_configs(self, ports):
        result = {}
        for port in ports:
            result[port] = self._int_config(port.label)
        return result

    def _int_config(self, interface):
        """Collect information about the specified interface

        Returns a dictionary from the output of ``show int sw <interface>``.
        """

        alternatives = [
            re.escape(r'More: <space>,  Quit: q or CTRL+Z, One line: <return> '),
            r'Classification rules:\r\n', # End
            r'[^ \t\r\n][^:]*:[^\n]*\n',       # Key:Value\r\n,
            r' [^\n]*\n',                        # continuation line (from k:v)
        ]
        self._sendline('show int sw %s' % interface)
        # Find the first Key:Value pair (this is needed to skip past some
        # possible matches for other patterns prior to this:
        self.console.expect(alternatives[2])

        k, v = self.console.after.split(':', 1)
        result = {k: v}

        while True:
            index = self.console.expect(alternatives)
            if index == 0:
                self.console.send(' ')
            elif index == 1:
                break
            elif index == 2:
                k, v = self.console.after.split(':', 1)
                result[k] = v
            elif index == 3:
                result[k] += self.console.after

        self.console.expect(self.main_prompt)
        return result
