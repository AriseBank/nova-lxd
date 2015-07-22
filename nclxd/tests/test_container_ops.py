# Copyright 2015 Canonical Ltd
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import ddt
import mock
from nova.compute import power_state
from nova import exception
from nova import test
from nova.virt import fake
from nova.virt import hardware
from pylxd import exceptions as lxd_exceptions
import six

from nclxd.nova.virt.lxd import container_ops
from nclxd.nova.virt.lxd import container_utils
from nclxd import tests


@ddt.ddt
@mock.patch.object(container_ops, 'CONF', tests.MockConf())
@mock.patch.object(container_utils, 'CONF', tests.MockConf())
class LXDTestContainerOps(test.NoDBTestCase):

    @mock.patch.object(container_utils, 'CONF', tests.MockConf())
    def setUp(self):
        super(LXDTestContainerOps, self).setUp()
        self.ml = tests.lxd_mock()
        lxd_patcher = mock.patch('pylxd.api.API',
                                 mock.Mock(return_value=self.ml))
        lxd_patcher.start()
        self.addCleanup(lxd_patcher.stop)

        self.container_ops = (
            container_ops.LXDContainerOperations(fake.FakeVirtAPI()))
        self.mc = mock.MagicMock()
        config_patcher = mock.patch.object(self.container_ops,
                                           'container_config',
                                           self.mc)
        config_patcher.start()
        self.addCleanup(config_patcher.stop)
        self.mv = mock.MagicMock()
        vif_patcher = mock.patch.object(self.container_ops,
                                        'vif_driver',
                                        self.mv)
        vif_patcher.start()
        self.addCleanup(vif_patcher.stop)

    def test_list_instances(self):
        self.assertEqual(['mock-instance-1', 'mock-instance-2'],
                         self.container_ops.list_instances())

    def test_list_instances_fail(self):
        self.ml.configure_mock(
            **{'container_list.side_effect':
               lxd_exceptions.APIError('Fake', 500)})
        self.assertRaises(
            exception.NovaException,
            self.container_ops.list_instances
        )

    @tests.annotated_data(
        ('exists', [True], exception.InstanceExists, False, 'mock-instance'),
        ('exists_rescue', [True], exception.InstanceExists,
         True, 'fake-instance'),
        ('fail', lxd_exceptions.APIError('Fake', 500),
         exception.NovaException, False, 'mock-instance')
    )
    def test_spawn_defined(self, tag, side_effect, expected, rescue, name):
        instance = tests.MockInstance()
        self.ml.container_defined.side_effect = side_effect
        self.assertRaises(
            expected,
            self.container_ops.spawn,
            {}, instance, {}, [], 'secret',
            name_label='fake-instance', rescue=rescue)
        self.ml.container_defined.called_once_with(name)

    def test_spawn_new(self):
        context = mock.Mock()
        instance = tests.MockInstance()
        image_meta = mock.Mock()
        injected_files = mock.Mock()
        network_info = mock.Mock()
        block_device_info = mock.Mock()
        self.ml.container_defined.return_value = False
        with mock.patch.object(self.container_ops, 'create_instance') as mc:
            self.assertEqual(
                None,
                self.container_ops.spawn(
                    context, instance, image_meta, injected_files, 'secret',
                    network_info, block_device_info, 'fake_instance', False))
            mc.assert_called_once_with(
                context, instance, image_meta, injected_files, 'secret',
                network_info, block_device_info, 'fake_instance', False)

    @mock.patch('oslo_utils.fileutils.ensure_tree',
                return_value=None)
    @mock.patch.object(container_ops, 'driver')
    def test_create_instance_swap(self, md, mt):
        instance = tests.MockInstance()
        block_device_info = mock.Mock()
        md.swap_is_usable.return_value = True
        self.assertRaises(
            exception.NovaException,
            self.container_ops.create_instance,
            {}, instance, {}, [], 'secret', None, block_device_info)
        mt.assert_called_once_with('/fake/instances/path/mock_instance')
        md.block_device_info_get_swap.assert_called_once_with(
            block_device_info)
        md.swap_is_usable.assert_called_once_with(
            md.block_device_info_get_swap.return_value)

    @mock.patch('oslo_utils.fileutils.ensure_tree',
                mock.Mock(return_value=None))
    @mock.patch.object(
        container_ops, 'driver',
        mock.Mock(swap_is_usable=mock.Mock(return_value=False)))
    def test_create_instance_ephemeral(self):
        instance = tests.MockInstance(ephemeral_gb=1)
        self.assertRaises(
            exception.NovaException,
            self.container_ops.create_instance,
            {}, instance, {}, [], 'secret', None, None)

    @mock.patch('oslo_utils.fileutils.ensure_tree',
                mock.Mock(return_value=None))
    @mock.patch.object(
        container_ops, 'driver',
        mock.Mock(swap_is_usable=mock.Mock(return_value=False)))
    @mock.patch.object(container_ops, 'configdrive')
    @tests.annotated_data(
        ('configdrive', False, None, True),
        ('network_info', False, mock.Mock(), False),
        ('rescue', True, None, False),
        ('configdrive_rescue', True, None, True),
        ('configdrive_network', False, mock.Mock(), False),
        ('network_rescue', True, mock.Mock(), False)
    )
    def test_create_instance(self, tag, rescue, network_info,
                             configdrive, mcd):
        context = mock.Mock()
        instance = tests.MockInstance()
        image_meta = mock.Mock()
        injected_files = mock.Mock()
        block_device_info = mock.Mock()

        mcd.required_by.return_value = configdrive
        self.ml.container_init.return_value = (
            200, {'operation': '/1.0/operations/0123456789'})

        with mock.patch.object(self.container_ops, 'start_instance') as ms:
            self.assertEqual(
                None,
                self.container_ops.create_instance(
                    context, instance, image_meta, injected_files, 'secret',
                    network_info, block_device_info, 'fake_instance', rescue))
            ms.assert_called_once_with(instance, network_info, rescue)
        self.mc.configure_container.assert_called_once_with(
            context, instance, image_meta, 'fake_instance', rescue)
        calls = [
            mock.call.container_init(self.mc.configure_container.return_value),
            mock.call.wait_container_operation('0123456789', 200, 20)
        ]
        self.assertEqual(calls, self.ml.method_calls[:2])
        name = rescue and 'fake_instance' or 'mock_instance'
        if configdrive:
            self.ml.container_update.assert_any_call(
                name,
                self.mc.configure_container_configdrive.return_value)
        if network_info:
            self.ml.container_update.assert_any_call(
                name,
                self.mc.configure_network_devices.return_value)
        if rescue:
            self.ml.container_update.assert_any_call(
                name,
                self.mc.configure_container_rescuedisk.return_value)

    @mock.patch.object(container_ops, 'utils')
    @tests.annotated_data(
        {'tag': 'rescue', 'rescue': True, 'is_neutron': False, 'timeout': 0},
        {'tag': 'running', 'running': True, 'is_neutron': False, 'timeout': 0},
        {'tag': 'neutron', 'timeout': 0},
        {'tag': 'neutron_timeout'},
        {'tag': 'neutron_unknown',
         'network_info': [{
             'id': '0123456789abcdef',
         }]},
        {'tag': 'neutron_active', 'network_info':
         [{
             'id': '0123456789abcdef',
             'active': True
         }]},
        {'tag': 'neutron_inactive',
         'network_info': [{
             'id': '0123456789abcdef',
             'active': False
         }],
         'vifs': ('0123456789abcdef',)},
        {'tag': 'neutron_multi',
         'network_info': [{
             'id': '0123456789abcdef',
         }, {
             'id': '123456789abcdef0',
             'active': True
         }, {
             'id': '23456789abcdef01',
             'active': False
         }],
         'vifs': ('23456789abcdef01',)},
        {'tag': 'neutron_failed',
         'network_info': [{
             'id': '0123456789abcdef',
             'active': False
         }],
         'vifs': ('0123456789abcdef',),
         'plug_side_effect': exception.VirtualInterfaceCreateException},
    )
    def test_start_instance(self, mu, tag='', rescue=False, running=False,
                            is_neutron=True, timeout=10, network_info=[],
                            vifs=(), plug_side_effect=None):
        instance = tests.MockInstance()
        self.ml.container_running.return_value = running
        self.ml.container_start.return_value = (
            200, {'operation': '/1.0/operations/0123456789'})
        container_ops.CONF.vif_plugging_timeout = timeout
        mu.is_neutron.return_value = is_neutron
        self.mv.plug.side_effect = plug_side_effect
        with mock.patch.object(self.container_ops.virtapi,
                               'wait_for_instance_event') as mw:
            self.assertEqual(
                None,
                self.container_ops.start_instance(instance,
                                                  network_info,
                                                  rescue))
            mw.assert_called_once_with(
                instance,
                [('network-vif-plugged', vif) for vif in vifs],
                deadline=timeout,
                error_callback=self.container_ops._neutron_failed_callback)
        self.assertEqual(
            [mock.call(instance, viface) for viface in network_info],
            self.mv.plug.call_args_list)
        calls = [
            mock.call.container_start(rescue and 'mock_instance-rescue'
                                      or 'mock_instance', 20),
            mock.call.wait_container_operation('0123456789', 200, 20)
        ]
        self.assertEqual(calls, self.ml.method_calls[-2:])

    def test_container_rescue_fail(self):
        context = mock.Mock()
        instance = tests.MockInstance()
        image_meta = mock.Mock()
        network_info = mock.Mock()
        self.ml.container_defined.return_value = True
        self.assertRaises(exception.NovaException,
                          self.container_ops.rescue, context,
                          instance,
                          network_info,
                          image_meta,
                          'secret')

    def test_container_rescue(self):
        context = mock.Mock()
        instance = tests.MockInstance()
        image_meta = mock.Mock()
        network_info = mock.Mock()
        self.ml.container_defined.return_value = False
        with mock.patch.object(self.container_ops, 'spawn') as ms:
            mgr = mock.Mock()
            mgr.attach_mock(ms, 'spawn')
            mgr.attach_mock(self.ml.container_stop, 'stop')
            self.assertEqual(None,
                             self.container_ops.rescue(context,
                                                       instance,
                                                       network_info,
                                                       image_meta,
                                                       'secret'))
            calls = [
                mock.call.stop('mock_instance', 20),
                mock.call.spawn(
                    context, instance, image_meta, [], 'secret', network_info,
                    name_label='mock_instance-rescue', rescue=True)
            ]
            self.assertEqual(calls, mgr.method_calls)

    def test_container_unrescue(self):
        instance = tests.MockInstance()
        network_info = mock.Mock()
        self.assertEqual(None,
                         self.container_ops.unrescue(instance,
                                                     network_info))
        calls = [
            mock.call.container_start('mock_instance', 20),
            mock.call.container_destroy('mock_instance-rescue')
        ]
        self.assertEqual(calls, self.ml.method_calls)

    @tests.annotated_data(
        ('RUNNING', power_state.RUNNING),
        ('STOPPED', power_state.SHUTDOWN),
        ('STARTING', power_state.NOSTATE),
        ('STOPPING', power_state.SHUTDOWN),
        ('ABORTING', power_state.CRASHED),
        ('FREEZING', power_state.PAUSED),
        ('FROZEN', power_state.SUSPENDED),
        ('THAWED', power_state.PAUSED),
        ('PENDING', power_state.NOSTATE),
        ('Success', power_state.RUNNING),
        ('UNKNOWN', power_state.NOSTATE),
        (lxd_exceptions.APIError('Fake', 500), power_state.NOSTATE),
    )
    def test_get_info(self, side_effect, expected):
        instance = tests.MockInstance()
        self.ml.container_state.side_effect = [side_effect]
        self.assertEqual(hardware.InstanceInfo(state=expected, num_cpu=2),
                         self.container_ops.get_info(instance))

    @mock.patch('six.moves.builtins.open')
    @mock.patch.object(container_ops.utils, 'execute')
    @mock.patch('pwd.getpwuid', mock.Mock(return_value=mock.Mock(pw_uid=1234)))
    @mock.patch('os.getuid', mock.Mock())
    def test_get_console_output(self, me, mo):
        instance = tests.MockInstance()
        context = mock.Mock()
        mo.return_value.__enter__.return_value = six.BytesIO(b'fake contents')
        self.assertEqual(b'fake contents',
                         self.container_ops.get_console_output(context,
                                                               instance))
        calls = [
            mock.call('chown', '1234:1234',
                      '/fake/lxd/root/containers/mock_instance/console.log',
                      run_as_root=True),
            mock.call('chmod', '755',
                      '/fake/lxd/root/containers/mock_instance',
                      run_as_root=True)
        ]
        self.assertEqual(calls, me.call_args_list)
