#!/usr/bin/env python3

# HTTP framework
from aiohttp import ClientSession, ClientTimeout, BasicAuth
from aiohttp import ClientResponseError, ClientConnectionError
from aiohttp import ServerTimeoutError
from aiohttp import web, hdrs
from aiohttp.web import HTTPBadGateway, HTTPGatewayTimeout
from aiohttp.web import HTTPBadRequest, HTTPInternalServerError
import asyncio, ssl

# Prometheus
import aioprometheus
from aioprometheus.renderer import render as prometheus_render
# Some helpers to create metrics about the requests
from request_metrics import requests_metrics_middleware, LatencyHistogram
# The usual suspects
import os, re

class GeneratorCollector:
    '''
    Mix-in class to make a collectors that gather metrics from a generator.
    The generator should return tuples of labels dict and value.

    To make the generator easier to implement we ignore any entry
    where the labels dict or the value is None, furthermore we filter
    the labels dict to remove any key with a None value.
    '''
    def get_all(self):
        for metric in self.generate():
            # Unpack here to ease debugging new metrics
            try:
                labels, value = metric
            except ValueError:
                raise ValueError(f'{self.name} value is not a tuple')
            if labels is not None and value is not None:
                labels = { k: v for k, v in labels.items()
                           if v is not None }
                yield labels, value

class GeneratorGauge(GeneratorCollector, aioprometheus.Gauge):
    def __init__(self, name, doc, generator, const_labels=None, registry=None):
        super().__init__(name, doc, const_labels, registry)
        self.generate = generator

class GeneratorCounter(GeneratorCollector, aioprometheus.Counter):
    def __init__(self, name, doc, generator, const_labels=None, registry=None):
        super().__init__(name, doc, const_labels, registry)
        self.generate = generator

pull_latency_hist = LatencyHistogram(
    "ros_exporter_pull_latency", "Latency pulling data from targets",
    buckets = (0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 15.0, 20.0, 30.0)
)

class RosExporterTarget:
    # The routes we read to create our metrics
    # Each entry is a group that get pulled in parallel
    #
    # Each request can cost some significant CPU time on the remote device,
    # which is turn falsify the CPU load reading. To minimize the impact
    # we read the CPU related routes one by one.
    routes = [
        [ '/system/resource' ],
        [ '/system/health' ],
        [
            '/interface/bonding',
            '/interface/ethernet',
            '/interface/wireguard',
            '/interface/vlan',
        ],
    ]

    def __init__(self, target, config):
        if 'username' not in config:
            raise ValueError('Target config must provide a username')

        self.target = target
        self.config = config
        self.request_options = {
            'auth': BasicAuth(config.get('username'), config.get('password', '')),
            'timeout': ClientTimeout(total=config.get('total_timeout', 120),
                                     sock_connect=config.get('connect_timeout', 10)),
            'raise_for_status': True,
        }

        if not config.get('ssl_validation', True):
            req_ssl = False
        elif config.get('ca_cert'):
            req_ssl = ssl.create_default_context(cafile=config.get('ca_cert'))
        else:
            req_ssl = None
        if req_ssl is not None:
            self.request_options['ssl'] = req_ssl

        schema = self.config.get('schema', 'https')
        address = self.config.get('address', self.target)
        self.baseurl = f'{schema}://{address}'

        self._ros_data = {}

        self.registry = aioprometheus.Registry()
        self.create_metrics()

    def get_ros_data(self, *path):
        obj = self._ros_data
        for p in path:
            if p == '*':
                return obj
            obj = obj[p]
        return obj['__content__']

    async def pull_route_data(self, client, route, ros_data):
        url = f'{self.baseurl}/rest{route}'
        for p in (r for r in route.split('/') if len(r) > 0):
            ros_data = ros_data.setdefault(p, {})

        labels = { 'target': self.target, 'route': route }
        with pull_latency_hist(labels):
            async with client.get(url, **self.request_options) as resp:
                ros_data['__content__'] = await resp.json()

    async def pull_data(self):
        # Pull the data from all routes
        ros_data = {}
        async with ClientSession() as client:
            for route_grp in self.routes:
                tasks = [ asyncio.create_task(self.pull_route_data(client, r, ros_data))
                          for r in route_grp ]
                try:
                    await asyncio.gather(*tasks)
                except:
                    for t in tasks:
                        t.cancel()
                    raise

        # Replace the data object with new results
        self._ros_data = ros_data

    def Gauge(self, name, doc, const_labels = None):
        generator = getattr(self, f'export_{name}')
        return GeneratorGauge(name, doc, generator, const_labels, self.registry)

    def Counter(self, name, doc, const_labels = None):
        generator = getattr(self, f'export_{name}')
        return GeneratorCounter(name, doc, generator, const_labels, self.registry)


    def create_metrics(self):
        self.create_system_resource_metrics()
        self.create_hwmon_metrics()
        self.create_network_metrics()

    # Some helper to deal with the router OS data
    def from_bool(self, v, true_val=1, false_val=0):
        if v is None:
            return None
        if v is True or v == "true":
            return true_val
        else:
            return false_val

    def if_not_none(self, v, op):
        return op(v) if v is not None else None

    def isum(self, *vals):
        vals = [ int(i) for i in vals if i is not None ]
        if len(vals) == 0:
            return None
        return sum(vals)

    #
    # Node info
    #
    def create_system_resource_metrics(self):
        self.Gauge('node_os_info', '')
        self.Gauge('node_mikrotik_info', '')
        self.Gauge('node_boot_time_seconds', '')
        self.Gauge('node_load1', '')
        self.Gauge('node_filesystem_free_bytes', '')
        self.Gauge('node_filesystem_size_bytes', '')

    def export_node_os_info(self):
        r = self.get_ros_data('system', 'resource')
        yield { 'id': r.get('platform'),
                 'version': r.get('version'),
                 'build_time': r.get('build-time')
               }, 1

    def export_node_mikrotik_info(self):
        r = self.get_ros_data('system', 'resource')
        keys = ('platform', 'board-name', 'cpu', 'cpu-count', 'factory-software')
        yield { k.replace('-', '_'): r.get(k) for k in keys
                if k in r }, 1

    uptime_re = re.compile('([0-9]+)([a-z])')
    uptime_units = {
        's': 1,
        'm': 60,
        'h': 3600,
        'd': 24 * 3600,
        'w': 7 * 24 * 3600,
    }

    def export_node_boot_time_seconds(self):
        try:
            txt = self.get_ros_data('system', 'resource')['uptime']
        except LookupError:
            return None
        secs = 0
        for count, unit in self.uptime_re.findall(txt):
            try:
                secs += int(count) * self.uptime_units[unit]
            except KeyError:
                raise ValueError(f'Invalid uptime: {txt}')
        yield {}, secs

    def export_node_load1(self):
        r = self.get_ros_data('system', 'resource')
        yield {}, int(r.get('cpu-count', 1)) * int(r.get('cpu-load', 0)) / 100

    def hdd_labels(self):
        return {
            'device': 'flash',
            'mountpoint': '/',
        }

    def export_node_filesystem_free_bytes(self):
        r = self.get_ros_data('system', 'resource')
        yield self.hdd_labels(), r.get('free-hdd-space')

    def export_node_filesystem_size_bytes(self):
        r = self.get_ros_data('system', 'resource')
        yield self.hdd_labels(), r.get('total-hdd-space')

    #
    # Hardware Monitoring
    #
    def create_hwmon_metrics(self):
        self.Gauge('node_hwmon_temp_celsius', '')
        self.Gauge('node_hwmon_in_volts', '')
        self.Gauge('node_hwmon_fan_rpm', '')

    def hwmon_labels(self, h):
        try:
            name = h['name']
        except LookupError:
            return None
        if name in ('temperature', 'voltage'):
            name = 'device'
        else:
            name = name.replace('-temperature', '')
            name = name.replace('-speed', '')
        return { 'sensor': name }

    def export_node_hwmon_value(self, t):
        for h in self.get_ros_data('system', 'health'):
            if h.get('type') == t:
                yield self.hwmon_labels(h), h.get('value')

    def export_node_hwmon_temp_celsius(self):
        return self.export_node_hwmon_value('C')

    def export_node_hwmon_in_volts(self):
        return self.export_node_hwmon_value('V')

    def export_node_hwmon_fan_rpm(self):
        return self.export_node_hwmon_value('RPM')

    #
    # Network interfaces
    #
    def create_network_metrics(self):
        self.Gauge('node_network_up', '')
        self.Gauge('node_network_carrier', '')
        self.Gauge('node_network_iface_id', '')
        self.Gauge('node_network_info', '')
        self.Gauge('node_network_mtu_bytes', '')
        self.Counter('node_network_receive_bytes_total', '')
        self.Counter('node_network_receive_drop_total', '')
        self.Counter('node_network_receive_errs_total', '')
        self.Counter('node_network_receive_fifo_total', '')
        self.Counter('node_network_receive_frame_total', '')
        self.Counter('node_network_receive_multicast_total', '')
        self.Counter('node_network_receive_broadcast_total', '')
        self.Counter('node_network_receive_unicast_total', '')
        self.Counter('node_network_receive_packets_total', '')
        self.Counter('node_network_transmit_bytes_total', '')
        self.Counter('node_network_transmit_colls_total', '')
        self.Counter('node_network_transmit_drop_total', '')
        self.Counter('node_network_transmit_multicast_total', '')
        self.Counter('node_network_transmit_broadcast_total', '')
        self.Counter('node_network_transmit_unicast_total', '')
        self.Counter('node_network_transmit_packets_total', '')

    def net_labels(self, iface):
        try:
            name = iface['name']
        except LookupError:
            return None
        return { 'device': name }

    def get_ros_interfaces(self):
        for itype in self.get_ros_data('interface', '*'):
            for iface in self.get_ros_data('interface', itype):
                yield iface, itype

    def interfaces_exporter(export):
        def wrapper(self):
            for iface, itype in self.get_ros_interfaces():
                yield export(self, iface)
        return wrapper

    @interfaces_exporter
    def export_node_network_up(self, iface):
        return self.net_labels(iface), \
            self.from_bool(iface.get('disabled'), 0, 1)

    @interfaces_exporter
    def export_node_network_carrier(self, iface):
        return self.net_labels(iface), \
            self.from_bool(iface.get('running'))

    @interfaces_exporter
    def export_node_network_iface_id(self, iface):
        return self.net_labels(iface), \
            int(iface['.id'].replace('*',''), 16)

    def export_node_network_info(self):
        for iface, itype in self.get_ros_interfaces():
            labels = self.net_labels(iface) | {
                'type': itype,
                'address': self.if_not_none(iface.get('mac-address'), str.lower),
                'duplex': self.from_bool(iface.get('full-duplex'), 'full', 'half'),
                'operstate': self.from_bool(iface.get('disabled'), 'down', 'up'),
                'switch': iface.get('switch'),
                'slaves': iface.get('slaves'),
                'comment': iface.get('comment'),
            }
            yield labels, 1

    @interfaces_exporter
    def export_node_network_mtu_bytes(self, iface):
        return self.net_labels(iface), iface.get('mtu')

    @interfaces_exporter
    def export_node_network_receive_bytes_total(self, iface):
        return self.net_labels(iface), iface.get('rx-bytes')

    @interfaces_exporter
    def export_node_network_receive_drop_total(self, iface):
        return self.net_labels(iface), iface.get('rx-drop')

    @interfaces_exporter
    def export_node_network_receive_errs_total(self, iface):
        return self.net_labels(iface), \
            self.isum(iface.get('rx-align-error'), iface.get('rx-fcs-error'))

    @interfaces_exporter
    def export_node_network_receive_fifo_total(self, iface):
        return self.net_labels(iface), iface.get('rx-overflow')

    @interfaces_exporter
    def export_node_network_receive_frame_total(self, iface):
        return self.net_labels(iface), iface.get('rx-fcs-error')

    @interfaces_exporter
    def export_node_network_receive_multicast_total(self, iface):
        return self.net_labels(iface), iface.get('rx-multicast')

    @interfaces_exporter
    def export_node_network_receive_broadcast_total(self, iface):
        return self.net_labels(iface), iface.get('rx-broadcast')

    @interfaces_exporter
    def export_node_network_receive_unicast_total(self, iface):
        return self.net_labels(iface), iface.get('rx-unicast')

    @interfaces_exporter
    def export_node_network_receive_packets_total(self, iface):
        return self.net_labels(iface), iface.get('rx-packet')

    @interfaces_exporter
    def export_node_network_transmit_bytes_total(self, iface):
        return self.net_labels(iface), iface.get('tx-bytes')

    @interfaces_exporter
    def export_node_network_transmit_colls_total(self, iface):
        return self.net_labels(iface), iface.get('tx-collision')

    @interfaces_exporter
    def export_node_network_transmit_drop_total(self, iface):
        return self.net_labels(iface), iface.get('tx-drop')

    @interfaces_exporter
    def export_node_network_transmit_multicast_total(self, iface):
        return self.net_labels(iface), iface.get('tx-multicast')

    @interfaces_exporter
    def export_node_network_transmit_broadcast_total(self, iface):
        return self.net_labels(iface), iface.get('tx-broadcast')

    @interfaces_exporter
    def export_node_network_transmit_unicast_total(self, iface):
        return self.net_labels(iface), iface.get('tx-unicast')

    @interfaces_exporter
    def export_node_network_transmit_packets_total(self, iface):
        return self.net_labels(iface), iface.get('tx-packet')


render_latency_hist = LatencyHistogram(
    "ros_exporter_render_latency", "Latency pulling data from targets",
    buckets = (0.0001, 0.0025, .005, 0.01, 0.1)
)

class RosExporter:
    def __init__(self, config=None):
        self.config = config or {}
        if 'username' not in self.config:
            self.config['username'] = \
                os.environ.get('ROS_EXPORTER_USERNAME', 'admin')
        if 'password' not in self.config:
            self.config['password'] = \
                os.environ.get('ROS_EXPORTER_PASSWORD', 'password')
        if 'ca_cert' not in self.config:
            self.config['ca_cert'] = \
                os.environ.get('ROS_EXPORTER_CA_CERT', None)
        if 'hosts' not in self.config:
            self.config['hosts'] = {}

        self.targets = {}

    def get_routes(self):
        return [
            web.get('/metrics', self.get_exporter_metrics, name="metrics"),
            web.get('/routeros', self.get_routeros_metrics, name="routeros"),
        ]

    def new_target(self, name):
        cfg = self.config.copy()
        del cfg['hosts']
        if name in self.config['hosts']:
            cfg.update(self.config['hosts'][name])

        return RosExporterTarget(name, cfg)

    def get_target(self, target):
        if target not in self.targets:
            return self.new_target(target)
        return self.targets[target]

    async def get_target_metrics(self, name, req):
        labels = { 'target': name, 'route': '__all__' }
        target = self.get_target(name)

        # Pull the data for this target
        with pull_latency_hist(labels):
            try:
                await target.pull_data()
            except ServerTimeoutError as err:
                text = str(err)
                print(f'{name}: {text}')
                raise HTTPGatewayTimeout(text=f'{text}\n')
            except ClientConnectionError as err:
                text = str(err)
                print(f'{name}: {text}')
                raise HTTPBadGateway(text=f'{text}\n')
            except ClientResponseError as err:
                info = err.request_info
                text = f'{info.method} {info.url} returned {err.status} - {err.message}'
                print(f'{name}: Response error: {text}')
                raise HTTPBadGateway(text=f'{text}\n')
            except asyncio.exceptions.TimeoutError:
                text = 'Timed out getting data'
                print(f'{name}: {text}')
                raise HTTPGatewayTimeout(text=f'{text}\n')
            except Exception as err:
                text = str(err) or type(err)
                text = f'Failed to get data: {text}'
                print(f'{name}: {text}')
                raise HTTPInternalServerError(text=f'{text}\n')

        # Render it according to the Accept header
        with render_latency_hist(labels):
            fmt = req.headers.getall(hdrs.ACCEPT, [])
            content, http_headers = prometheus_render(target.registry, fmt)

        # Add the target to the list once it succeeded
        self.targets[name] = target
        return web.Response(body=content, headers=http_headers)

    def get_exporter_metrics(self, req):
        fmt = req.headers.getall(hdrs.ACCEPT, [])
        content, http_headers = prometheus_render(aioprometheus.REGISTRY, fmt)
        return web.Response(body=content, headers=http_headers)

    async def get_routeros_metrics(self, req):
        if 'target' not in req.query:
            raise HTTPBadRequest(text='The `target` parameter is missing in the query\n')
        return await self.get_target_metrics(req.query['target'], req)

def app(*args, **kwargs):
    exporter = RosExporter(*args, **kwargs)
    app = web.Application()
    app.middlewares.insert(0, requests_metrics_middleware)
    app.add_routes(exporter.get_routes())
    return app

if __name__ == '__main__':
    import argparse, yaml

    parser = argparse.ArgumentParser(description="RouterOS Node Exporter")
    parser.add_argument('--port', type=int, default=9729)
    parser.add_argument('--config')
    parser.add_argument('--username')
    parser.add_argument('--password')
    parser.add_argument('--ca-cert')
    parser.add_argument('--total-timeout', type=float)
    parser.add_argument('--connect-timeout', type=float)
    parser.add_argument('--ssl-validation', action=argparse.BooleanOptionalAction)

    args = parser.parse_args()

    # Read the yaml config if there is one
    if args.config:
        with open(args.config) as fd:
            config = yaml.full_load(fd)
        # TODO: Validate the config
    else:
        config = {}

    # Apply the command line parameters on top
    config.update({ k: v for k, v in vars(args).items()
                    if k != 'config' and v is not None })

    web.run_app(app(config), port=args.port)
