import json
import logging

from logs import JsonFormatter, configure_logging


def _record(**extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        "x.y", logging.INFO, __file__, 1, "hi %s", ("there",), None
    )
    record.__dict__.update(extra)
    return record


def test_record_is_one_json_line_with_workgraph_keys() -> None:
    line = JsonFormatter().format(_record())
    assert "\n" not in line
    assert set(json.loads(line)) == {"time", "level", "logger", "message"}
    assert json.loads(line)["message"] == "hi there"


def test_exc_with_exc_info() -> None:
    err = ValueError("boom")
    record = _record()
    record.exc_info = (ValueError, err, None)
    assert "ValueError: boom" in json.loads(JsonFormatter().format(record))["exc"]


def test_object_from_k8s_ref() -> None:
    ns = _record(k8s_ref={"name": "a", "namespace": "n"})
    assert json.loads(JsonFormatter().format(ns))["object"] == "n/a"
    cluster = _record(k8s_ref={"name": "a", "namespace": None})
    assert json.loads(JsonFormatter().format(cluster))["object"] == "a"


def test_configure_logging_leaves_one_json_handler() -> None:
    root = logging.getLogger()
    saved = root.handlers[:]
    try:
        root.addHandler(logging.StreamHandler())
        configure_logging()
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, JsonFormatter)
    finally:
        root.handlers[:] = saved
