# Run by test_hidden_condition_labels.py in its own process (it stubs langchain modules):
# the answers and eval writers still store with_docs / without_docs. Exits non-zero otherwise.
import json
import sys
import types
from pathlib import Path
from unittest.mock import Mock, patch

for mod_name in ("langchain_openai", "langchain_anthropic"):
    if mod_name not in sys.modules:
        m = types.ModuleType(mod_name)
        m.ChatOpenAI = Mock
        m.ChatAnthropic = Mock
        sys.modules[mod_name] = m

import dokimi.eval.answerer as A  # noqa: E402
import dokimi.eval.judge as J  # noqa: E402
from dokimi.artifacts import validate_artifact  # noqa: E402

tmp = Path(sys.argv[1])
mcp = Mock()
mcp.get_library_docs.return_value = [{
    "content": "parallel_for: use tbb::parallel_for(range, body) to parallelize a loop over a range.",
    "source": "context7", "library_id": "uxlfoundation/oneTBB"}]
usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
with patch.object(A, "llm_call_with_usage", return_value=("Use tbb::parallel_for.", usage)), \
     patch.object(A, "LANGCHAIN_AVAILABLE", True), \
     patch.object(A, "SENTENCE_TRANSFORMERS_AVAILABLE", False), \
     patch.object(A, "ChatOpenAI", Mock(), create=True):
    answerer = A.Answerer(mcp_client=mcp, model="m", provider="openai", rerank_threshold=0.0)
    answers = answerer.generate_answers(
        "oneTBB", "uxlfoundation/oneTBB",
        [{"id": "q1", "text": "How do I use parallel_for to parallelize a loop?"}])
    answerer.save_answers(answers, tmp / "answers" / "oneTBB.json")
adata = json.loads((tmp / "answers" / "oneTBB.json").read_text(encoding="utf-8"))
rec = adata["answers"][0]
assert isinstance(rec.get("with_docs"), dict) and isinstance(rec.get("without_docs"), dict), sorted(rec)
assert rec["with_docs"].get("answer") and rec["without_docs"].get("answer"), rec
summ = adata.get("token_usage_summary", {})
assert "with_docs" in summ and "without_docs" in summ, sorted(summ)
validate_artifact("answers", adata)

scores = {"correctness": 80, "completeness": 80, "specificity": 80, "code_quality": 80,
          "actionability": 80, "aggregate": 80, "reasoning": {}}
llm = Mock()
llm.invoke.return_value = Mock(content=json.dumps(scores))
with patch.object(J, "ChatAnthropic", Mock(return_value=llm), create=True), \
     patch.object(J, "LANGCHAIN_AVAILABLE", True):
    judge = J.Judge()
    evals = judge.evaluate_answers("oneTBB", adata["answers"])
    judge.save_evaluations(evals, tmp / "eval" / "oneTBB.json")
edata = json.loads((tmp / "eval" / "oneTBB.json").read_text(encoding="utf-8"))
erec = edata["evaluations"][0]
assert isinstance(erec.get("with_docs"), dict) and isinstance(erec.get("without_docs"), dict), sorted(erec)
assert erec["with_docs"].get("aggregate") is not None, erec
validate_artifact("eval", edata)
