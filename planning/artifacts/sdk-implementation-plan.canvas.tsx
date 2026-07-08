import {
  Stack, Row, Grid, H1, H2, H3, Text, Card, CardHeader, CardBody,
  Table, Callout, Divider, Pill, Stat, Code, Spacer,
  CollapsibleSection, useHostTheme,
} from "cursor/canvas";

// ─── Data ───────────────────────────────────────────────────────────────────

const phases = [
  {
    id: 1,
    title: "Architecture & API Design",
    duration: "1–2 weeks",
    kind: "Design",
    summary:
      "Pure design work — no code. Produces the OpenAPI spec that all subsequent phases implement against. Nothing else should be built until this spec is agreed upon.",
    deliverables: [
      "Unified Platform API OpenAPI spec — all 15 endpoints with schemas",
      "Tower Gateway internal API spec — contracts for the proxy layer",
      "Base URL structure and versioning scheme (e.g. /v1/)",
      "Error response schema — especially the tower-offline 503 shape",
      "Auth model decision documented (flagged below — do not skip this)",
    ],
    flags: [],
  },
  {
    id: 2,
    title: "Tower Gateway Expansion",
    duration: "1–2 weeks",
    kind: "Build",
    summary:
      "Changes to infra/tower-dashboard/gateway.py on the Jetson. The gateway becomes the internal proxy target for the control plane — not the SDK surface. SDK consumers never call it directly.",
    deliverables: [
      "GET /api/snapshot/cam{n} — new endpoint, ffmpeg single-frame JPEG from local RTSP",
      "Move binding from 127.0.0.1:8766 to wg0 interface (VPN-accessible, not internet)",
      "Refactor PTZ endpoints to clean REST shape matching the Platform API proxy spec",
      "Structured JSON error responses on all endpoints (currently inconsistent)",
    ],
    flags: [
      "Binding change: verify iptables rules still restrict port 8766 from non-wg0 interfaces after the move",
    ],
  },
  {
    id: 3,
    title: "Control Plane API Expansion",
    duration: "2–3 weeks",
    kind: "Build",
    summary:
      "Extends infra/enrollment-api/app/main.py (FastAPI) to become the unified SDK-facing API. Two categories: fleet management (registry reads/writes) and tower proxy (HTTP forwarding over WireGuard to tower gateway).",
    deliverables: [
      "Fleet endpoints — customers, towers, groups (registry reads + tower registration)",
      "Tower proxy middleware — async HTTP client forwarding to tower VPN IP on wg0",
      "Connection pooling, configurable timeout, and graceful degradation when tower is offline",
      "OpenAPI schema export at /openapi.json for the docs site generator",
    ],
    flags: [
      "Proxy adds ~50–100ms VPN round-trip latency to PTZ and snapshot calls — acceptable but must be stated in SDK docs",
      "Tower offline handling must return HTTP 503 with structured JSON body before any SDK code is written against it",
    ],
  },
  {
    id: 4,
    title: "SDK Package — kallon-sdk (new repo)",
    duration: "2 weeks",
    kind: "Build",
    summary:
      "New repository: kallon-sdk. Python package installable via pip. No imports from the main repo — SDK communicates only over published network APIs. Serves both internal dashboard team and external enterprise integrators.",
    deliverables: [
      "KallonClient — typed wrapper for the entire control plane API",
      "AlertVerifier — HMAC-SHA256 verification utility for webhook consumers",
      "Typed models for all request and response shapes (dataclasses or Pydantic)",
      "TowerOfflineError, AuthError, APIError exception hierarchy",
      "PyPI-ready pyproject.toml with semantic versioning",
      "Unit test suite for client and AlertVerifier",
    ],
    flags: [],
  },
  {
    id: 5,
    title: "Documentation Site",
    duration: "2–3 weeks",
    kind: "Write",
    summary:
      "Docusaurus site inside the kallon-sdk repo. API reference auto-generated from the OpenAPI spec. Prose guides written by hand. This is the DoLynk-equivalent developer portal.",
    deliverables: [
      "API reference — auto-generated from /openapi.json, one page per resource",
      "Quick-start guide — dashboard team integration from zero to first API call",
      "Alert webhook guide — HMAC verification with Python, JavaScript, and curl examples",
      "RTSP consumption guide — VPN peer model, URL pattern, codec requirements",
      "Tower bring-up guide — Terra ops audience (see structure below)",
      "Code examples in Python, JavaScript, and curl for every API operation",
    ],
    flags: [
      "Bring-up guide hardware section: confirm Jetson Orin NX is the production unit before writing the wiring diagram — wiring differs from Orin Nano bench unit",
    ],
  },
];

const controlPlaneEndpoints: [string, string, string, string, "New" | "Exists"][] = [
  ["GET",  "/v1/customers",                              "List all customers + hub metadata",          "Fleet",        "New"],
  ["GET",  "/v1/customers/{customer_id}",                "Get customer detail, VPN subnet, hub info",  "Fleet",        "New"],
  ["GET",  "/v1/customers/{customer_id}/towers",         "List towers for a customer",                 "Fleet",        "New"],
  ["GET",  "/v1/towers",                                 "List all towers (Terra ops)",                "Fleet",        "New"],
  ["GET",  "/v1/towers/{device_id}",                     "Tower detail — VPN IP, state, enrollment",   "Fleet",        "New"],
  ["POST", "/v1/towers",                                 "Register a new tower (factory flow)",        "Fleet",        "New"],
  ["POST", "/v1/towers/{device_id}/ptz/move",            "PTZ absolute or continuous move (proxied)",  "Tower proxy",  "New"],
  ["POST", "/v1/towers/{device_id}/ptz/stop",            "PTZ stop (proxied)",                        "Tower proxy",  "New"],
  ["GET",  "/v1/towers/{device_id}/ptz/status",          "Current PTZ position (proxied)",             "Tower proxy",  "New"],
  ["GET",  "/v1/towers/{device_id}/snapshot/cam{n}",     "JPEG still frame (proxied via ffmpeg)",      "Tower proxy",  "New"],
  ["GET",  "/v1/towers/{device_id}/status",              "Sensor + health snapshot (proxied)",         "Tower proxy",  "New"],
  ["GET",  "/v1/towers/{device_id}/streams",             "RTSP stream readiness (proxied)",            "Tower proxy",  "New"],
  ["POST", "/v1/enroll",                                 "Tower first-boot enrollment",                "Enrollment",   "Exists"],
  ["POST", "/v1/enroll/confirm",                         "Confirm WireGuard handshake post-enroll",    "Enrollment",   "Exists"],
  ["GET",  "/healthz",                                   "API liveness probe",                         "Infra",        "Exists"],
];

const towerGatewayEndpoints: [string, string, string, "New" | "Exists"][] = [
  ["GET",  "/api/snapshot/cam{n}", "JPEG frame via ffmpeg from local RTSP stream",     "New"],
  ["POST", "/api/ptz",             "Relay PTZ command to daemon on 127.0.0.1:8765",    "Exists"],
  ["GET",  "/api/status",          "Watchdog sensor/health JSON (proxied from :8770)", "Exists"],
  ["GET",  "/api/streams",         "mediamtx path readiness (proxied from :9997)",     "Exists"],
  ["GET",  "/api/config",          "Device ID + camera list from device.env",          "Exists"],
  ["GET",  "/api/events",          "SSE alert stream",                                 "Exists"],
  ["POST", "/ingest/alerts",       "Local alert mirror sink",                          "Exists"],
  ["GET",  "/healthz",             "Liveness",                                         "Exists"],
];

const sdkRepoStructure: [string, string][] = [
  ["kallon_sdk/__init__.py",   "Public exports"],
  ["kallon_sdk/client.py",     "KallonClient — unified API wrapper"],
  ["kallon_sdk/models.py",     "Typed request/response models"],
  ["kallon_sdk/alerts.py",     "AlertVerifier — HMAC verification for webhook consumers"],
  ["kallon_sdk/exceptions.py", "TowerOfflineError, AuthError, APIError"],
  ["tests/",                   "Unit tests — client and AlertVerifier"],
  ["pyproject.toml",           "Package metadata, deps, semantic version"],
  ["docs/",                    "Docusaurus site — API ref + guides"],
  ["docs/api/",                "Auto-generated from OpenAPI spec"],
  ["docs/guides/",             "Quick-start, alerts, RTSP, tower bring-up"],
];

const bringUpStages: [string, string, string][] = [
  ["Hardware",        "Sensor wiring, camera power, switch VLAN",               "i2cdetect -y 7 shows 0x68; ping 192.168.10.108 from Jetson succeeds"],
  ["Preflight",       "device.env present, arm64 confirmed, root verified",      "00-preflight.sh exits 0"],
  ["Install",         "kallon-jetson-install.sh modules 00–99",                  "99-acceptance.sh exits 0 — all assertions green"],
  ["Network policy",  "WAN/camera NIC separation enforced",                      "ip route get 1.1.1.1 → wlP1p1s0; ip route get camera IP → enP8p1s0"],
  ["WireGuard",       "Tunnel up, hub peer added via enrollment API",             "wg show wg0: latest handshake < 30s ago"],
  ["RTSP",            "Stream reachable from NOC VPN peer",                      "ffprobe rtsp://tower-vpn-ip:8554/cam1 returns stream info"],
  ["Alerts",          "HMAC alert accepted at hub listener",                     "Hub listener logs: POST /alerts 200 OK"],
  ["PTZ",             "Daemon responding to JSON commands",                       "ping command returns {ok: true} within 2s"],
];

// ─── Component ───────────────────────────────────────────────────────────────

export default function SDKPlan() {
  const { tokens: t } = useHostTheme();

  return (
    <Stack gap={32} style={{ padding: 28, maxWidth: 980, margin: "0 auto" }}>

      {/* ── Header ── */}
      <Stack gap={6}>
        <H1>Kallon SDK — Implementation Plan</H1>
        <Text tone="secondary">
          Unified Platform API · kallon-sdk Python package · Docusaurus developer portal · Tower bring-up guide
        </Text>
      </Stack>

      {/* ── Core Architecture Decision ── */}
      <Callout tone="info" title="Core architectural decision: unified control plane API">
        The SDK calls one base URL — the Terra control plane API. Tower-specific operations (PTZ, snapshot, sensor
        status) are proxied by the control plane over WireGuard to the tower gateway on each Jetson. Fleet data
        (customers, towers, groups) is served directly from the Postgres registry. SDK consumers never call
        the tower directly. RTSP live video is the only exception — it cannot be HTTP-proxied and requires
        a VPN peer (documented in the SDK, flagged as a future transcoding relay opportunity).
      </Callout>

      {/* ── Summary stats ── */}
      <Grid columns={4} gap={16}>
        <Stat value="5"  label="Phases" />
        <Stat value="12" label="New API endpoints" />
        <Stat value="2"  label="Repos (main + kallon-sdk)" />
        <Stat value="8"  label="Weeks estimated total" />
      </Grid>

      <Divider />

      {/* ── Phases ── */}
      <H2>Implementation Phases</H2>

      <Stack gap={10}>
        {phases.map((p) => (
          <Stack gap={0} style={{ display: "contents" }}>
            <Card collapsible defaultOpen={p.id <= 3}>
              <CardHeader
                trailing={
                  <Row gap={8} align="center">
                    <Text size="small" tone="secondary">{p.duration}</Text>
                    <Pill size="sm" active={p.kind === "Build"}>{p.kind}</Pill>
                  </Row>
                }
              >
                Phase {p.id} — {p.title}
              </CardHeader>
              <CardBody>
                <Stack gap={14}>
                  <Text tone="secondary">{p.summary}</Text>

                  <Stack gap={6}>
                    <Text size="small" weight="semibold">Deliverables</Text>
                    <Stack gap={3}>
                      {p.deliverables.map((d) => (
                        <Row gap={8} align="start">
                          <Text tone="tertiary" size="small" style={{ minWidth: 10, marginTop: 1 }}>—</Text>
                          <Text size="small">{d}</Text>
                        </Row>
                      ))}
                    </Stack>
                  </Stack>

                  {p.flags.length > 0 && (
                    <Stack gap={6}>
                      {p.flags.map((f) => (
                        <Callout tone="warning">{f}</Callout>
                      ))}
                    </Stack>
                  )}
                </Stack>
              </CardBody>
            </Card>
          </Stack>
        ))}
      </Stack>

      <Divider />

      {/* ── Control Plane API ── */}
      <Stack gap={10}>
        <Stack gap={3}>
          <H2>Control Plane API — Endpoint Map</H2>
          <Text tone="secondary">
            The SDK-facing surface. Extends the existing enrollment FastAPI service at <Code>infra/enrollment-api/app/main.py</Code>.
            12 new endpoints + 3 existing.
          </Text>
        </Stack>
        <Table
          headers={["Method", "Path", "Purpose", "Type", "Status"]}
          rows={controlPlaneEndpoints.map(([method, path, purpose, type, status]) => [
            <Pill size="sm" active={method === "POST"}>{method}</Pill>,
            <Code>{path}</Code>,
            <Text size="small">{purpose}</Text>,
            <Text size="small" tone="secondary">{type}</Text>,
            <Pill size="sm" active={status === "New"}>{status}</Pill>,
          ])}
          columnAlign={["left", "left", "left", "left", "center"]}
          striped
          stickyHeader
        />
      </Stack>

      <Divider />

      {/* ── Tower Gateway ── */}
      <Stack gap={10}>
        <Stack gap={3}>
          <H2>Tower Gateway API — Internal Endpoint Map</H2>
          <Text tone="secondary">
            Runs on each Jetson at <Code>infra/tower-dashboard/gateway.py</Code>, bound to <Code>wg0</Code> after Phase 2.
            Not called by SDK consumers directly — used by the control plane proxy layer only. One new endpoint.
          </Text>
        </Stack>
        <Table
          headers={["Method", "Path", "Purpose", "Status"]}
          rows={towerGatewayEndpoints.map(([method, path, purpose, status]) => [
            <Pill size="sm" active={method === "POST"}>{method}</Pill>,
            <Code>{path}</Code>,
            <Text size="small">{purpose}</Text>,
            <Pill size="sm" active={status === "New"}>{status}</Pill>,
          ])}
          columnAlign={["left", "left", "left", "center"]}
          striped
        />
      </Stack>

      <Divider />

      {/* ── SDK repo ── */}
      <Stack gap={10}>
        <Stack gap={3}>
          <H2>New Repository: kallon-sdk</H2>
          <Text tone="secondary">
            Separate repo. Python package + Docusaurus docs site. No imports from the main repo.
            Installable via <Code>pip install kallon-sdk</Code>. Satisfies the "decouple" requirement.
          </Text>
        </Stack>
        <Table
          headers={["Path", "Purpose"]}
          rows={sdkRepoStructure.map(([path, purpose]) => [
            <Code>{path}</Code>,
            <Text size="small">{purpose}</Text>,
          ])}
          striped
        />
      </Stack>

      <Divider />

      {/* ── Bring-up guide structure ── */}
      <Stack gap={10}>
        <Stack gap={3}>
          <H2>Tower Bring-Up Guide — Stage Structure</H2>
          <Text tone="secondary">
            Audience: Terra ops / manufacturing technicians. Structured around observable checkpoints — each stage
            has an exact expected output so a technician can confirm pass/fail without interpretation.
          </Text>
        </Stack>
        <Table
          headers={["Stage", "What you verify", "Pass looks like"]}
          rows={bringUpStages.map(([stage, verify, pass_]) => [
            <Text size="small" weight="semibold">{stage}</Text>,
            <Text size="small" tone="secondary">{verify}</Text>,
            <Code style={{ fontSize: 11 }}>{pass_}</Code>,
          ])}
          striped
        />
        <Callout tone="neutral">
          Each stage also needs a "Common failures" sub-table: symptom → probable cause → fix command.
          This is the most valuable part of the guide for field support — it is what transforms a setup
          document into a commissioning tool.
        </Callout>
      </Stack>

      <Divider />

      {/* ── Open decisions ── */}
      <H2>Decisions to Resolve Before Shipping Externally</H2>

      <Stack gap={8}>
        <Callout tone="danger" title="Authentication — breaking change if deferred too long">
          The control plane API currently has no auth beyond the enrollment token flow. Before the SDK is
          used by any external integrator, every fleet and proxy endpoint needs an auth layer.
          API key (stored in device.env pattern) is lowest friction. JWT issued by the control plane
          is more powerful for multi-tenant use. Adding auth after the SDK is published is a breaking
          change — decide this during Phase 1 even if implementation is deferred.
        </Callout>

        <Callout tone="warning" title="RTSP cannot be proxied through the control plane API">
          Live video streams (<Code>{"rtsp://tower-vpn-ip:8554/camN"}</Code>) are not HTTP — they cannot
          be relayed through the API proxy. SDK consumers who need live video must be WireGuard VPN peers
          or the platform must provide a transcoding relay (HLS/MJPEG). Document this constraint
          explicitly in the SDK quick-start. Flag as a future platform capability (Phase 8 territory).
        </Callout>

        <Callout tone="warning" title="Tower offline handling — define the error contract in Phase 1">
          When the control plane proxies a request to a tower and gets no response (tunnel down, Jetson
          rebooting), the API must return a structured error. Suggested: HTTP 503 with a JSON body
          containing <Code>{"{ \"error\": \"tower_offline\", \"device_id\": \"...\" }"}</Code>.
          Define this schema in Phase 1 before any proxy code is written — SDK clients must handle it.
        </Callout>

        <Callout tone="neutral" title="JavaScript SDK — defer until after Python is tested on a real deployment">
          Most OpenAPI generators (hey-api, openapi-generator) can produce a typed TypeScript client
          from the spec with minimal handwork. Prioritise Python + the documentation site first.
          Schedule JS after the Python SDK has been exercised against a live tower.
        </Callout>
      </Stack>

    </Stack>
  );
}
