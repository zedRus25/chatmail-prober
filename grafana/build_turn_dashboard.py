"""Generate grafana/dashboard-turn.json.

Run: `uv run python grafana/build_turn_dashboard.py` to refresh the
dashboard JSON.  Kept as a generator (rather than hand-rolled JSON) so
the helper module stays the single source of truth for panel styling.
"""

from __future__ import annotations

import json
from pathlib import Path


# Status value-mappings shared between the TURN and iroh state-timelines.
_TURN_STATUS_MAPPINGS = {
    "1":  {"text": "ok",        "color": "green"},
    "0":  {"text": "down",      "color": "red"},
    "-2": {"text": "parse-err", "color": "orange"},
    "-4": {"text": "no-binary", "color": "purple"},
    "-5": {"text": "timeout",   "color": "yellow"},
}

_IROH_STATUS_MAPPINGS = {
    "1":  {"text": "ok",           "color": "green"},
    "0":  {"text": "down",         "color": "red"},
    "-2": {"text": "no-metadata",  "color": "orange"},
    "-3": {"text": "imap-failed",  "color": "purple"},
    "-5": {"text": "timeout",      "color": "yellow"},
}


_SINGLE_DASH_URL = (
    "/d/chatmail-single/relay-network3a-single-relay?var-relay="
)


def state_timeline_panel(title, description, metric, legend_format,
                         status_mappings, gridPos):
    """Per-relay state-timeline of an integer status gauge."""
    return {
        "title": title,
        "description": description,
        "type": "state-timeline",
        "datasource": {"type": "prometheus", "uid": "${datasource}"},
        "gridPos": gridPos,
        "fieldConfig": {
            "defaults": {
                "custom": {"lineWidth": 0, "fillOpacity": 70},
                "mappings": [{"type": "value", "options": status_mappings}],
                "links": [{
                    "title": "Single relay view",
                    "url": _SINGLE_DASH_URL + "${__field.labels.relay}",
                }],
            },
        },
        "options": {
            "mergeValues": True,
            "showValue": "auto",
            "alignValue": "left",
            "rowHeight": 0.9,
            "legend": {"displayMode": "list", "placement": "bottom"},
        },
        "targets": [{
            "refId": "A",
            "expr": metric,
            "legendFormat": legend_format,
        }],
    }


def pie_panel(title, description, slices, gridPos):
    """Pie chart of category counts.

    `slices` is a list of (label, color, expr) tuples.  Each expr should
    yield a single scalar count.  Fixed colours per label so the legend
    stays stable even when a slice collapses to zero.
    """
    targets = [
        {"refId": chr(ord("A") + i), "expr": expr, "legendFormat": label,
         "instant": True}
        for i, (label, _color, expr) in enumerate(slices)
    ]
    overrides = [
        {"matcher": {"id": "byName", "options": label},
         "properties": [
             {"id": "color", "value": {"mode": "fixed", "fixedColor": colour}},
         ]}
        for label, colour, _expr in slices
    ]
    return {
        "title": title,
        "description": description,
        "type": "piechart",
        "datasource": {"type": "prometheus", "uid": "${datasource}"},
        "gridPos": gridPos,
        "fieldConfig": {
            "defaults": {
                "unit": "short",
                "color": {"mode": "palette-classic"},
                "mappings": [],
            },
            "overrides": overrides,
        },
        "options": {
            "pieType": "pie",
            "displayLabels": ["value"],
            "legend": {"displayMode": "table", "placement": "right",
                        "values": ["value", "percent"]},
            "reduceOptions": {"calcs": ["lastNotNull"], "values": False,
                              "fields": ""},
            "tooltip": {"mode": "single"},
        },
        "targets": targets,
    }


def turn_breakdown_panel(gridPos):
    """4-way pie: self ok/failing, fallback ok/failing."""
    return pie_panel(
        title="TURN breakdown",
        description=(
            "Relays grouped by TURN endpoint kind (self-published vs "
            "fallback turn.delta.chat) and health."
        ),
        slices=[
            ("self ok",          "green",
             'count(cmping_relay_turn_status{turn_endpoint="self"} == 1)'),
            ("self failing",     "red",
             'count(cmping_relay_turn_status{turn_endpoint="self"} != 1)'),
            ("fallback ok",      "yellow",
             'count(cmping_relay_turn_status{turn_endpoint="fallback"} == 1)'),
            ("fallback failing", "orange",
             'count(cmping_relay_turn_status{turn_endpoint="fallback"} != 1)'),
        ],
        gridPos=gridPos,
    )


def iroh_breakdown_panel(gridPos):
    """5-way pie of iroh status categories."""
    return pie_panel(
        title="iroh breakdown",
        description=(
            "Relays grouped by iroh-relay status. "
            "no-metadata = server doesn't advertise an iroh URL; "
            "imap-failed = could not fetch metadata."
        ),
        slices=[
            ("ok",          "green",
             "count(cmping_relay_iroh_status == 1)"),
            ("down",        "red",
             "count(cmping_relay_iroh_status == 0)"),
            ("no-metadata", "orange",
             "count(cmping_relay_iroh_status == -2)"),
            ("imap-failed", "purple",
             "count(cmping_relay_iroh_status == -3)"),
            ("timeout",     "yellow",
             "count(cmping_relay_iroh_status == -5)"),
        ],
        gridPos=gridPos,
    )


def failing_table_panel(title, description, expr, status_mappings,
                        extra_labels, gridPos):
    """Table of currently-failing series.

    `expr` should be an instant query that returns one sample per
    failing relay (e.g. `cmping_relay_turn_status != 1`).  The
    `status_mappings` dict maps integer status -> human label so the
    Value column renders readably.  `extra_labels` lists label columns
    to keep alongside `relay` (e.g. ["turn_endpoint"] for TURN, [] for
    iroh).
    """
    keep_fields = ["relay", *extra_labels, "Value"]
    mapping_options = {
        str(k): {"text": v["text"], "color": v["color"]}
        for k, v in status_mappings.items()
    }
    return {
        "title": title,
        "description": description,
        "type": "table",
        "datasource": {"type": "prometheus", "uid": "${datasource}"},
        "gridPos": gridPos,
        "fieldConfig": {
            "defaults": {
                "custom": {"align": "left", "displayMode": "auto"},
                "mappings": [{"type": "value", "options": mapping_options}],
                "color": {"mode": "thresholds"},
            },
            "overrides": [
                {"matcher": {"id": "byName", "options": "relay"},
                 "properties": [
                     {"id": "links", "value": [{
                         "title": "Single relay view",
                         "url": _SINGLE_DASH_URL + "${__data.fields.relay}",
                     }]},
                 ]},
                {"matcher": {"id": "byName", "options": "Value"},
                 "properties": [
                     {"id": "custom.displayMode", "value": "color-background"},
                     {"id": "displayName", "value": "status"},
                 ]},
            ],
        },
        "options": {
            "showHeader": True,
            "sortBy": [{"displayName": "relay", "desc": False}],
        },
        "targets": [{
            "refId": "A",
            "expr": expr,
            "instant": True,
            "format": "table",
        }],
        "transformations": [
            {"id": "filterFieldsByName",
             "options": {"include": {"names": keep_fields}}},
        ],
    }


def by_relay_timeseries(title, expr, unit, gridPos, description="",
                        legend_format="{{relay}}", log_scale=False):
    """Generic per-relay timeseries (one line per relay)."""
    custom = {"lineWidth": 1, "fillOpacity": 5, "spanNulls": True}
    if log_scale:
        custom["scaleDistribution"] = {"type": "log", "log": 10}
    return {
        "title": title,
        "description": description,
        "type": "timeseries",
        "datasource": {"type": "prometheus", "uid": "${datasource}"},
        "gridPos": gridPos,
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "custom": custom,
            },
        },
        "options": {
            "tooltip": {"mode": "multi", "sort": "desc"},
            "legend": {"displayMode": "table", "placement": "right",
                        "calcs": ["lastNotNull"]},
        },
        "targets": [{
            "refId": "A",
            "expr": expr,
            "legendFormat": legend_format,
        }],
    }


def row(title, gridPos, collapsed=False):
    return {
        "type": "row",
        "title": title,
        "collapsed": collapsed,
        "gridPos": gridPos,
        "panels": [],
    }


def build_dashboard():
    panels = []
    y = 0

    panels.append(row("TURN", {"h": 1, "w": 24, "x": 0, "y": y}))
    y += 1
    panels.append(state_timeline_panel(
        title="TURN availability",
        description=(
            "Per-relay TURN endpoint health. "
            "1=ok, 0=down, -2=parse-err, -4=no-binary, -5=timeout."
        ),
        metric="cmping_relay_turn_status",
        legend_format="{{relay}} ({{turn_endpoint}})",
        status_mappings=_TURN_STATUS_MAPPINGS,
        gridPos={"h": 18, "w": 12, "x": 0, "y": y},
    ))
    panels.append(turn_breakdown_panel({"h": 18, "w": 6, "x": 12, "y": y}))
    panels.append(failing_table_panel(
        title="TURN failing right now",
        description=(
            "Relays whose latest TURN probe is not OK, including which "
            "endpoint kind (self vs fallback) was tested. Empty = all healthy."
        ),
        expr="cmping_relay_turn_status != 1",
        status_mappings=_TURN_STATUS_MAPPINGS,
        extra_labels=["turn_endpoint"],
        gridPos={"h": 18, "w": 6, "x": 18, "y": y},
    ))
    y += 18

    panels.append(row("TURN timing", {"h": 1, "w": 24, "x": 0, "y": y}))
    y += 1
    panels.append(by_relay_timeseries(
        "Connect time (TURN allocate)", "cmping_relay_turn_connect_seconds", "s",
        {"h": 8, "w": 12, "x": 0, "y": y},
        description=(
            "Time to establish the TURN allocation, per relay, Loopback measurement. "
        ),
        legend_format="{{relay}} ({{turn_endpoint}})",
    ))
    panels.append(by_relay_timeseries(
        "Transmit time", "cmping_relay_turn_transmit_seconds", "s",
        {"h": 8, "w": 12, "x": 12, "y": y},
        description=(
            "Total loopback test transmit duration. "
        ),
        legend_format="{{relay}} ({{turn_endpoint}})",
    ))
    y += 8

    panels.append(row("iroh", {"h": 1, "w": 24, "x": 0, "y": y}))
    y += 1
    panels.append(state_timeline_panel(
        title="iroh availability",
        description=(
            "Per-relay iroh-relay HTTP health. "
            "1=ok, 0=down, -2=no-metadata, -3=imap-failed, -5=timeout."
        ),
        metric="cmping_relay_iroh_status",
        legend_format="{{relay}}",
        status_mappings=_IROH_STATUS_MAPPINGS,
        gridPos={"h": 18, "w": 12, "x": 0, "y": y},
    ))
    panels.append(iroh_breakdown_panel({"h": 18, "w": 6, "x": 12, "y": y}))
    panels.append(failing_table_panel(
        title="iroh failing right now",
        description=(
            "Relays whose latest iroh-relay probe is not OK. "
            "Empty = all healthy."
        ),
        expr="cmping_relay_iroh_status != 1",
        status_mappings=_IROH_STATUS_MAPPINGS,
        extra_labels=[],
        gridPos={"h": 18, "w": 6, "x": 18, "y": y},
    ))
    y += 18

    panels.append(by_relay_timeseries(
        "iroh latency (per relay)", "cmping_relay_iroh_latency_seconds", "s",
        {"h": 10, "w": 24, "x": 0, "y": y},
        description=(
            "Last iroh-relay HTTP probe latency. "
            "One sample per round (~15 min cadence). Log y-axis."
        ),
        log_scale=True,
    ))

    return {
        "title": "Chatmail TURN + iroh health",
        "uid": "chatmail-turn",
        "tags": ["chatmail", "turn", "iroh"],
        "schemaVersion": 39,
        "timezone": "",
        "refresh": "1m",
        "time": {"from": "now-6h", "to": "now"},
        "templating": {
            "list": [
                {
                    "name": "datasource",
                    "type": "datasource",
                    "query": "prometheus",
                    "current": {},
                },
                {
                    "name": "relay",
                    "type": "query",
                    "datasource": {"type": "prometheus", "uid": "${datasource}"},
                    # Union via metric-name regex so a relay appearing
                    # in only one of {turn,iroh} still shows up.  Using
                    # `group by (...) (... or ...)` would be cleaner but
                    # the Prometheus datasource's label_values() second
                    # form rejects PromQL aggregations -- it accepts a
                    # bare series selector only.
                    "query": (
                        "label_values("
                        '{__name__=~"cmping_relay_(turn|iroh)_status"}, '
                        "relay)"
                    ),
                    "refresh": 2,
                    "includeAll": True,
                    "multi": True,
                    "current": {"text": "All", "value": "$__all"},
                },
            ],
        },
        "panels": panels,
    }


def main():
    dashboard = build_dashboard()
    out = Path(__file__).parent / "dashboard-turn.json"
    out.write_text(json.dumps(dashboard, indent=2) + "\n")
    print(f"wrote {out} ({len(dashboard['panels'])} panels)")


if __name__ == "__main__":
    main()
