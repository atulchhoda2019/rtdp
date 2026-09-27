"""gRPC server: InferenceService.Score/Warm per proto/rtdp/v1/services.proto.

Wraps model output in the canonical SignalEnvelope. Inference never decides
outcomes, transitions state, or dispatches actions (ADR-011); it produces
contracted signal evidence only.
"""

import os
import sys
import time
import uuid
from concurrent import futures
from datetime import datetime, timezone
from pathlib import Path

import grpc

# Generated protobuf code (gen/python) — make proto first.
_here = Path(__file__).resolve()
_cands = [
    Path(os.environ.get("RTDP_GEN_PYTHON", "/nonexistent")),
    Path("/app/gen/python"),                                   # container
]
for _depth in (1, 2, 3, 4):                                    # repo checkout
    if len(_here.parents) > _depth:
        _cands.append(_here.parents[_depth] / "gen" / "python")
for _cand in _cands:
    if _cand.is_dir():
        sys.path.insert(0, str(_cand))
        break
from rtdp.v1 import envelope_pb2, services_pb2, services_pb2_grpc  # noqa: E402

from .scorer import ModelNotReady, ModelRegistry  # noqa: E402

MODE_NAMES = {0: "MODE_UNSPECIFIED", 1: "LIVE", 2: "SHADOW", 3: "REPLAY"}
STATUS_OK = envelope_pb2.SIGNAL_STATUS_OK


def _ts(dt: datetime):
    from google.protobuf.timestamp_pb2 import Timestamp
    t = Timestamp()
    t.FromDatetime(dt)
    return t


class InferenceServicer(services_pb2_grpc.InferenceServiceServicer):
    def __init__(self, registry: ModelRegistry):
        self.registry = registry

    def Warm(self, req, ctx):
        try:
            lm = self.registry.warm(
                model_id=req.model_id, model_version=req.model_version,
                model_digest=req.model_digest, artifact_uri=req.artifact_uri,
                input_schema_digest=req.input_schema_digest,
                preprocessing_digest=req.preprocessing_digest)
            return services_pb2.WarmResponse(ready=True, model_digest=lm.digest)
        except Exception as e:  # ModelNotReady, fetch/digest errors
            return services_pb2.WarmResponse(ready=False, error=str(e))

    def Score(self, req, ctx):
        started = time.monotonic()
        now = datetime.now(timezone.utc)
        remaining_ms = req.deadline_ms
        try:
            lm = self.registry.get(req.model_digest)
            values = [self._scalar(v) for v in req.feature_values]
            prob = lm.predict_proba(values)
            status = STATUS_OK
            out_values = {"probability": prob}
            quality = envelope_pb2.SignalQuality()
        except ModelNotReady:
            status = envelope_pb2.SIGNAL_STATUS_UNAVAILABLE
            out_values = {}
            quality = envelope_pb2.SignalQuality()
        except ValueError:
            status = envelope_pb2.SIGNAL_STATUS_INVALID_INPUT
            out_values = {}
            quality = envelope_pb2.SignalQuality()
        if status != STATUS_OK:
            prob = None

        # Signal name/version come from the warmed model's declared output
        # contract; the digest is pinned by the caller's binding spec.
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
            producer_id="inference-service",
            model_id=req.model_id,
            model_version=req.model_version,
            model_digest=req.model_digest,
            preprocessing_digest=req.preprocessing_digest,
            input_snapshot_digest=req.input_snapshot_digest,
            event_time=req.event_time,
            computed_at=_ts(now),
            expires_at=_ts(now.fromtimestamp(
                now.timestamp() + max(remaining_ms, 1) / 1000.0,
                tz=timezone.utc)),
            status=status,
            quality=quality,
            traceparent=req.traceparent,
        )
        if status == STATUS_OK:
            env.values["probability"].double_value = prob
            env.contract_digest = req.contract_digest
        return services_pb2.ScoreResponse(envelope=env)

    @staticmethod
    def _scalar(v: envelope_pb2.TypedValue) -> float:
        which = v.WhichOneof("kind")
        if which == "double_value":
            return float(v.double_value)
        if which == "int_value":
            return float(v.int_value)
        raise ValueError(f"feature value must be numeric, got {which}")


def serve():
    port = int(os.environ.get("RTDP_INFERENCE_PORT", "50051"))
    workers = int(os.environ.get("RTDP_INFERENCE_WORKERS", "4"))
    registry = ModelRegistry()
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=workers),
        options=[("grpc.max_receive_message_length", 4 * 1024 * 1024)])
    services_pb2_grpc.add_InferenceServiceServicer_to_server(
        InferenceServicer(registry), server)
    server.add_insecure_port(f"[::]:{port}")
    server.start()
    print(f"inference-service listening on :{port}", flush=True)
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
