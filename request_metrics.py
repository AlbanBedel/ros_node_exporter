
from aioprometheus import Counter, Gauge, Histogram
from aiohttp import web
import asyncio, time

class LatencyContextManager:
    def __init__(self, measure_time):
        self.measure_time = measure_time

    def __enter__(self):
        self.start_time = time.time()

    def __exit__(self, type, value, traceback):
        self.measure_time(time.time() - self.start_time)

class LatencyHistogram(Histogram):
    def __call__(self, labels):
        # Use a private copy of the labels
        labels = type(labels)(labels)
        return LatencyContextManager(lambda t: self.add(labels, t))

class SimpleContextManager:
    def __init__(self, on_enter, on_exit):
        self.on_enter = on_enter
        self.on_exit = on_exit

    def __enter__(self):
        self.on_enter()

    def __exit__(self, type, value, traceback):
        self.on_exit()

class InProgressGauge(Gauge):
    def __call__(self, labels):
        # Use a private copy of the labels
        labels = type(labels)(labels)
        return SimpleContextManager(lambda: self.inc(labels),
                                    lambda: self.dec(labels))

requests_counter = Counter(
    "requests_total", "Total Request Count"
)

requests_connection_reset_counter = Counter(
    "requests_connection_reset_total",
    "Total Number of Requests where the connection was reset"
)

requests_cancelled_counter = Counter(
    "requests_cancelled_total",
    "Total Number of Requests that were cancelled"
)

requests_latency_hist = LatencyHistogram(
    "requests_latency_seconds", "Request latency"
)

requests_in_progress_gauge = InProgressGauge(
    "requests_in_progress_total", "Requests currently in progress"
)

requests_exceptions_counter = Counter(
    "requests_exceptions_total", "Total Number of Exceptions during Requests"
)

@web.middleware
async def requests_metrics_middleware(request: web.Request, handler) -> web.Response:
    start_time = time.time()
    route = request.match_info.route.name
    labels = {"method": request.method, "route": route}
    with requests_in_progress_gauge(labels), requests_latency_hist(labels):
        try:
            response = await handler(request)
            labels["status"] = response.status
            requests_counter.inc(labels)
        except web.HTTPException as e:
            labels["status"] = e.status_code
            requests_counter.inc(labels)
            raise
        except ConnectionResetError:
            requests_connection_reset_counter.inc(labels)
            raise
        except asyncio.CancelledError:
            requests_cancelled_counter.inc(labels)
            raise
        except Exception:
            requests_exceptions_counter.inc(labels)
            raise
    return response
