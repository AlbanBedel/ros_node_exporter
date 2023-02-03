# RouterOS Node Exporter

This is a simple prometheus exporter for Mikrotik RouterOS. Unlike most
other prometheus exporter for Mikrotik devices it use the [multi-target
exporter pattern](https://prometheus.io/docs/guides/multi-target-exporter/)
to support multiple devices with a single instance.

Also unlike most other implementation it provides metrics compatible
with the node exporter. This avoid the need to create a whole new
set of alerts and visualization.

RouterOS specific metrics could be added later.

## Configuration

The RouterOS devices must have the REST API enabled for use with this
exporter. It is recommended that you configure a dedicated user with
only API read permissions on the RouterOS devices.

The exporter can be configured from the command line, environment
variables, or a config file. The following environment variables
are supported:

  * ROS_EXPORTER_USERNAME: The username used to access the devices.
  * ROS_EXPORTER_PASSWORD: The password used to access the devices.
  * ROS_EXPORTER_CA_CERT: The CA certificate used to validate the
    devices certificate.

It is possible to set per device settings using a config file,
see the example in `config.yml`.

## Scraping with prometheus

On the prometheus side you need to use relabling to redirect prometheus
to the ROS node exporter. Assuming it run on the same host as prometheus
you should define a prometheus job with the following `metric_path` and
`relabel_configs`:

	job_name: ros_node
	static_configs:
	  - targets:
		- ros-switch01.local
		- ros-switch02.local
	metrics_path: /routeros
	relabel_configs:
	  - source_labels: [__address__]
		target_label: __param_target
	  - source_labels: [__param_target]
		target_label: instance
	  - target_label: __address__
		replacement: localhost:9729

For an in-depth explanation of this configuration please see the
[multi-target exporter guide](https://prometheus.io/docs/guides/multi-target-exporter/#querying-multi-target-exporters-with-prometheus) in the prometheus documentation.

The ROS node exporter itself also has some metrics about itself.
You can also create  job to track them:

	job_name: ros_node_exporter
	static_configs:
	  - targets:
		- localhost:9729

