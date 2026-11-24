import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch

path = Path(__file__).resolve().parents[1] / "skill/scripts/deploy_workbuddy_ccswitch.py"
spec = importlib.util.spec_from_file_location("deploy_wbcc", path)
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class InferenceProbeTests(unittest.TestCase):
    def stream(self, *frames):
        return io.BytesIO(b"".join(b"data: " + frame + b"\n\n" for frame in frames))

    def test_complete_stream(self):
        stream = self.stream(
            b'{"model":"chosen","choices":[{"delta":{"content":"WBCC_OK"},"finish_reason":null}]}',
            b'{"model":"chosen","choices":[{"delta":{},"finish_reason":"stop"}]}',
            b'[DONE]',
        )
        first, total = deploy.check_inference_stream(stream, "chosen", 0)
        self.assertLessEqual(first, total)

    def test_missing_done_is_incomplete(self):
        stream = self.stream(
            b'{"model":"chosen","choices":[{"delta":{"content":"WBCC_OK"},"finish_reason":"stop"}]}',
        )
        with self.assertRaisesRegex(ValueError, "DONE"):
            deploy.check_inference_stream(stream, "chosen", 0)

    def test_done_with_partial_tool_is_incomplete(self):
        stream = self.stream(
            b'{"model":"chosen","choices":[{"delta":{"tool_calls":[{"index":0}]},"finish_reason":null}]}',
            b'[DONE]',
        )
        with self.assertRaisesRegex(ValueError, "stop"):
            deploy.check_inference_stream(stream, "chosen", 0)

    def test_rejects_stream_without_reported_model(self):
        stream = self.stream(
            b'{"choices":[{"delta":{"content":"WBCC_OK"},"finish_reason":"stop"}]}',
            b'[DONE]',
        )
        with self.assertRaisesRegex(ValueError, "model"):
            deploy.check_inference_stream(stream, "chosen", 0)

    def test_heartbeat_does_not_extend_deadline(self):
        stream = io.BytesIO(b": heartbeat\n\n")
        with patch.object(deploy.time, "monotonic", return_value=11):
            with self.assertRaises(TimeoutError):
                deploy.check_inference_stream(stream, "chosen", 0, deadline=10)

    def test_rejects_error_and_wrong_model(self):
        for frame, error in (
            (b'{"error":{"message":"upstream error"}}', "SSE"),
            (b'{"model":"other","choices":[]}', "model"),
        ):
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                deploy.check_inference_stream(self.stream(frame), "chosen", 0)


if __name__ == "__main__":
    unittest.main()
