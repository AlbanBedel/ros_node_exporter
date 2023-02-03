FROM python:3.10-alpine
RUN pip3 --no-cache-dir install aiohttp aioprometheus pyyaml

# Create a non privildged user
ARG USER=ros-node-exporter
RUN adduser -H -D $USER

# Copy the python modules and executable
ARG LIBDIR=/usr/local/lib/ros-node-exporter
ENV PYTHONPATH=$LIBDIR
ENV PYTHONUNBUFFERED=1

COPY request_metrics.py $LIBDIR/
COPY ros_node_exporter.py /usr/local/bin

# Configure the container
USER $USER
ENTRYPOINT [ "ros_node_exporter.py" ]
