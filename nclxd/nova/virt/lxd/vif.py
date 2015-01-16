import getpass

import os

from oslo.config import cfg

from nova.i18n import _LW
from nova import exception
from nova import utils
from nova.openstack.common import log as logging
from nova.network import linux_net
from nova.network import model as network_model

from . import utils as container_utils


CONF = cfg.CONF

LOG = logging.getLogger(__name__)


def write_lxc_config(instance, vif):
    config_file = os.path.join(CONF.lxd.lxd_root_dir,
                               instance['uuid'],
                               'config')
    with open(config_file, 'a+') as f:
        f.write('lxc.network.type = veth\n')
        f.write('lxc.network.hwaddr = %s\n' % vif['address'])
        if vif['type'] == 'ovs':
            bridge = 'qbr%s' % vif['id'][:11]
        else:
            bridge = vif['network']['bridge']

        f.write('lxc.network.link = %s\n' % bridge)
            

class LXDGenericDriver(object):
    def _get_vif_driver(self, vif):
        vif_type = vif['type']
        if vif_type is None:
            raise exception.NovaException(
                _("vif_type parameter must be present "
                  "for this vif_driver implementation"))
        elif vif_type == network_model.VIF_TYPE_OVS:
            return LXDOpenVswitchDriver()
        else:
            return LXDNetworkBridgeDriver()

    def plug(self, instance, vif):
        vif_driver = self._get_vif_driver(vif)
        vif_driver.plug(instance, vif)

    def unplug(self, instance, vif):
        vif_driver = self._get_vif_driver(vif)
        vif_driver.unplug(instance, vif)

class LXDOpenVswitchDriver(object):
    def plug(self, instance, vif):
        iface_id = self._get_ovs_interfaceid(vif)
        br_name = self._get_br_name(vif['id'])
        v1_name, v2_name = self._get_veth_pair_names(vif['id'])

        if not linux_net.device_exists(br_name):
            utils.execute('brctl', 'addbr', br_name, run_as_root=True)
            utils.execute('brctl', 'setfd', br_name, 0, run_as_root=True)
            utils.execute('brctl', 'stp', br_name, 'off', run_as_root=True)
            utils.execute('tee',
                  ('/sys/class/net/%s/bridge/multicast_snooping' %
                    br_name),
                    process_input='0',
                    run_as_root=True,
                    check_exit_code=[0, 1])

        if not linux_net.device_exists(v2_name):
            linux_net._create_veth_pair(v1_name, v2_name)
            utils.execute('ip', 'link', 'set', br_name, 'up', run_as_root=True)
            utils.execute('brctl', 'addif', br_name, v1_name, run_as_root=True)
            linux_net.create_ovs_vif_port(self._get_bridge_name(vif),
                                          v2_name, iface_id, vif['address'],
                                          instance['uuid'])

        write_lxc_config(instance, vif)
        container_utils.write_lxc_usernet(instance, br_name)

    def unplug(self, instance, vif):
        try:
            br_name = self.get_br_name(vif['id'])
            v1_name, v2_name = self.get_veth_pair_names(vif['id'])

            if linux_net.device_exists(br_name):
                utils.execute('brctl', 'delif', br_name, v1_name,
                              run_as_root=True)
                utils.execute('ip', 'link', 'set', br_name, 'down',
                              run_as_root=True)
                utils.execute('brctl', 'delbr', br_name,
                              run_as_root=True)

            linux_net.delete_ovs_vif_port(self._get_bridge_name(vif),
                                          v2_name)
        except processutils.ProcessExecutionError:
            LOG.exception(_("Failed while unplugging vif"),
                         instance=instance)

    def _get_bridge_name(self, vif):
        return vif['network']['bridge']

    def _get_ovs_interfaceid(self, vif):
        return vif.get('ovs_interfaceid') or vif['id']

    def _get_br_name(self, iface_id):
        return ("qbr" + iface_id)[:network_model.NIC_NAME_LEN]

    def _get_veth_pair_names(self, iface_id):
        return (("qvb%s" % iface_id)[:network_model.NIC_NAME_LEN],
                ("qvo%s" % iface_id)[:network_model.NIC_NAME_LEN])

class LXDNetworkBridgeDriver(object):
    def plug(self, contianer, instance, vif):
        network = vif['network']
        if (not network.get_meta('multi_host', False) and
                network.get_meta('should_create_bridge', False)):
            if network.get_meta('should_create_vlan', False):
               iface = CONF.vlan_interface or \
                 network.get_meta('bridge_interface')
               LOG.debug('Ensuring vlan %(vlan)s and bridge %(bridge)s',
                        {'vlan': network.get_meta('vlan'),
                        'bridge': vif['network']['bridge']},
                        instance=instance)
               linux_net.LinuxBridgeInterfaceDriver.ensure_vlan_bridge(
                    network.get_meta('vlan'),
                    vif['network']['bridge'],
                    iface)
            else:
                iface = CONF.flat_interface or \
                    network.get_meta('bridge_interface')
                LOG.debug("Ensuring bridge %s",
                          vif['network']['bridge'], instance=instance)
                linux_net.LinuxBridgeInterfaceDriver.ensure_bridge(
                    vif['network']['bridge'],
                    iface)
        write_lxc_config(instance, vif)

    def unplug(self, container, intsance, vif):
        pass