"""gRPC server: same InferenceService.Score/Warm surface as the ONNX
service, backed by a pinned SLM runtime (Ollama locally; a managed
endpoint on AWS).

Generative output is validated against the contract before it becomes
signal evidence — a malformed or out-of-range completion is INVALID_INPUT,
never a fabricated value. Inference never decides outcomes or dispatches
actions (ADR-011).
"""

import os
import sys
import time
import uuid
from concurrent import futures
from datetime import datetime, timezone
from pathlib import Path

import grpc

_here = Path(__file__).resolve()
_cands = [
    Path(os.environ.get("RTDP_GEN_PYTHON", "/nonexistent")),
    Path("/app/gen/python"),
]
for _depth in (1, 2, 3, 4):
    if len(_here.parents) > _depth:
        _cands.append(_here.parents[_depth] / "gen" / "python")
for _cand in _cands:
    if _cand.is_dir():
        sys.path.insert(0, str(_cand))
        break
from rtdp.v1 import envelope_pb2, services_pb2, services_pb2_grpc  # noqa: E402

from .slm import ModelNotReady, SlmRegistry  # noqa: E402

STATUS_OK = envelope_pb2.SIGNAL_STATUS_OK


def _ts(dt: datetime):
    from google.protobuf.timestamp_pb2 import Timestamp
    t = Timestamp()
    t.FromDatetime(dt)
    return t


class SlmServicer(services_pb2_grpc.InferenceServiceServicer):
    def __init__(self, registry: SlmRegistry):
        self.registry = registry

    def Warm(self, req, ctx):
        try:
            lm = self.registry.warm(
                model_id=req.model_id, model_version=req.model_version,
                model_digest=req.model_digest, artifact_uri=req.artifact_uri,
                input_schema_digest=req.input_schema_digest,
                preprocessing_digest=req.preprocessing_digest)
            return services_pb2.WarmResponse(ready=True, model_digest=lm.digest)
        except Exception as e:
            return services_pb2.WarmResponse(ready=False, error=str(e))

    def Score(self, req, ctx):
        now = datetime.now(timezone.utc)
        timeout_s = max(req.deadline_ms, 1000) / 1000.0
        try:
            lm = self.registry.get(req.model_digest)
            variables = {n: self._scalar(v)
                         for n, v in zip(req.feature_names,
                                         req.feature_values)}
            out = lm.generate(variables, timeout_s)
            out_name = lm.meta.get("output_value", "consistency")
            val = float(out[out_name])
            if not (0.0 <= val <= 1.0):
                raise ValueError(f"{out_name} out of range: {val}")
            status = STATUS_OK
            out_values = {out_name: val}
        except ModelNotReady:
            status = envelope_pb2.SIGNAL_STATUS_UNAVAILABLE
            out_values = {}
        except (ValueError, KeyError):
            # Generative output failed schema validation — data, not crash.
            status = envelope_pb2.SIGNAL_STATUS_INVALID_INPUT
            out_values = {}
        except Exception:
            status = envelope_pb2.SIGNAL_STATUS_TIMED_OUT
            out_values = {}

        contract_ref = ""
        if status == STATUS_OK:
            contract_ref = lm.meta.get("output_contract") or ""
        signal_name, _, contract_version = contract_ref.partition("@")

        env = envelope_pb2.SignalEnvelope(
            envelope_version="1",
            signal_event_id="sig_" + uuid.uuid4().hex[:16],
            tenant_id=req.tenant_id,
            environment=req.environment,
            mode=req.mode,
            transaction_id=req.transaction_id,
            transaction_revision=req.transaction_revision,
            decision_context_id=req.decision_context_id,
            signal_name=signal_name,
            contract_version=contract_version,
            binding_id=req.binding_id,
            binding_version=req.binding_version,
            producer_id="slm-service",
            model_id=req.model_id,
            model_version=req.model_version,
            model_digest=req.model_digest,
            preprocessing_digest=req.preprocessing_digest,
            input_snapshot_digest=req.input_snapshot_digest,
            event_time=req.event_time,
            computed_at=_ts(now),
            expires_at=_ts(datetime.fromtimestamp(
                now.timestamp() + timeout_s, tz=timezone.utc)),
            status=status,
            quality=envelope_pb2.SignalQuality(),
            traceparent=req.traceparent,
        )
        if status == STATUS_OK:
            for k, v in out_values.items():
                env.values[k].double_value = v
            env.contract_digest = req.contract_digest
        return services_pb2.ScoreResponse(envelope=env)

    @staticmethod
    def _scalar(v: envelope_pb2.TypedValue):
        which = v.WhichOneof("kind")
        if which == "double_value":
            return float(v.double_value)
        if which == "int_value":
            return float(v.int_value)
        if which == "string_value":
            # attr.<key> request attributes carry document text into prompt
            # templates — generate() renders it with str().
            return v.string_value
        raise ValueError(f"feature value must be numeric or string, got {which}")


def serve():
    port = int(os.environ.get("RTDP_SLM_PORT", "50056"))
    workers = int(os.environ.get("RTDP_SLM_WORKERS", "2"))
    registry = SlmRegistry()
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=workers),
        options=[("grpc.max_receive_message_length", 4 * 1024 * 1024)])
    services_pb2_grpc.add_InferenceServiceServicer_to_server(
        SlmServicer(registry), server)
    server.add_insecure_port(f"[::]:{port}")
    server.start()
    print(f"slm-service listening on :{port}", flush=True)
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
