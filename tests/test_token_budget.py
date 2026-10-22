import sys
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))

from factory.adapters.token_budget import count_request_tokens


class TokenBudgetTests(unittest.TestCase):
    def test_unmapped_configured_models_use_provider_counter(self):
        for model in ("configured-model-a", "configured-model-b"):
            messages = [{"role": "user", "content": "Hello 世界"}]
            counter = Mock(return_value=25)
            with patch("tiktoken.encoding_for_model", side_effect=KeyError(model)):
                count = count_request_tokens(
                    messages, model=model, remote_counter=counter
                )
            self.assertEqual(count, 25)
            counter.assert_called_once_with(model, messages)

    def test_remote_count_still_enforces_total_budget(self):
        with patch("tiktoken.encoding_for_model", side_effect=KeyError):
            with self.assertRaisesRegex(ValueError, "total budget"):
                count_request_tokens(
                    [],
                    model="configured-model",
                    remote_counter=Mock(return_value=100),
                    max_context_tokens=100,
                    max_completion_tokens=10,
                )

    def test_unknown_model_without_counter_remains_blocked(self):
        with self.assertRaisesRegex(ValueError, "no known tokenizer"):
            count_request_tokens([], model="unknown-model")

    def test_remote_errors_and_invalid_counts_fail_closed(self):
        for result in (True, -1, "12", None):
            with (
                self.subTest(result=result),
                patch("tiktoken.encoding_for_model", side_effect=KeyError),
            ):
                with self.assertRaisesRegex(ValueError, "invalid token count"):
                    count_request_tokens(
                        [],
                        model="configured-model",
                        remote_counter=Mock(return_value=result),
                    )
        with patch("tiktoken.encoding_for_model", side_effect=KeyError):
            with self.assertRaisesRegex(
                ValueError, "Provider token counting failed"
            ) as error:
                count_request_tokens(
                    [],
                    model="configured-model",
                    remote_counter=Mock(side_effect=RuntimeError("private response")),
                )
            self.assertNotIn("private response", str(error.exception))

    def test_known_models_do_not_use_remote_counter(self):
        counter = Mock()
        self.assertGreater(
            count_request_tokens(
                [{"role": "user", "content": "Hello"}], remote_counter=counter
            ),
            0,
        )
        counter.assert_not_called()
