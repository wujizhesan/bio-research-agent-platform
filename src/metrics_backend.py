"""Metrics classes for long-lived services and isolated tool children."""

import os


if os.environ.get('BIO_AGENT_ISOLATED_TOOL_CHILD') == '1':
    class _NoopMetric:
        def __init__(self, *args, **kwargs):
            pass

        def labels(self, *args, **kwargs):
            return self

        def inc(self, *args, **kwargs):
            pass

        def dec(self, *args, **kwargs):
            pass

        def set(self, *args, **kwargs):
            pass

        def observe(self, *args, **kwargs):
            pass

    Counter = Gauge = Histogram = _NoopMetric
else:
    from prometheus_client import Counter, Gauge, Histogram
