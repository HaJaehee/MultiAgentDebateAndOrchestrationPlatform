"""System 1 결정 LLM 호출기 (`app/agents/decision.py`): Jev 및 Laya `/v1/systemone` 단일 패스 평가.

기존 에이전트 발언·합성이 자기회귀 생성형 모델(System 2, LiteLLM)을 쓰는 것과 달리,
`decision_llm.enabled = true` 로 켜진 에이전트는 단일 포워드 패스(Single Forward Pass)로
`noul`(이진 확률)·`choice`(다지선다)·`score`(다단계 척도 점수) 질문 묶음을 동시에 판정합니다.

지원 백엔드:
- **Jev** (TypeSafe AI): 기본 `https://api.typesafe.ai/v1/systemone` HTTP 엔드포인트
- **Laya** (Convai Innovations): `api_base`(`LAYA_API_BASE`) 지정 시 HTTP 엔드포인트(`/v1/systemone` 또는 `/predict`),
  미지정 시 인프로세스 Python `laya.Router` 실행

보안 및 계측:
- API 키는 `DecisionLLMConfig.effective_api_key` 를 통해 `.env` (`JEV_API_KEY`, `LAYA_API_KEY`, `DECISION_API_KEY`)
  를 1차 참조하고 `conf.json` 오버라이드를 우선 적용합니다.
- 모든 호출은 `get_llm_gate().slot()` 동시성 상한과 `llm_usage` 토큰·호출 시간 계측을 거칩니다.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Literal, Mapping, Optional, Sequence, Tuple

import httpx

from app.agents import llm_usage
from app.agents.base import Agent
from app.agents.llm_gate import get_llm_gate
from app.agents.llm_types import LLMUnavailableError
from app.config import DecisionLLMConfig

logger = logging.getLogger(__name__)

QuestionKind = Literal["noul", "choice", "score"]

# 결정 장부 미해결 쟁점 머리의 고정 번호 패턴 (`app.orchestration.ledger_history.ISSUE_TAG`와 동일한 정규식).
# 순환 참조 방지를 위해 app.agents 계층에 독립적으로 정의합니다.
_ISSUE_TAG = re.compile(r"^\[I-(\d{1,5})\]\s*")

# 단일 패스 입력 텍스트 상한 (과도한 프롬프트로 인한 결정 모델 컨텍스트 초과 방지)
MAX_DECISION_STATE_CHARS = 32_000
# choice 선택지 하나에 붙이는 설명의 길이 상한. 쟁점 본문 전체는 state 에 이미 들어 있습니다.
MAX_CHOICE_NOTE_CHARS = 300


class DecisionHTTPStatusError(RuntimeError):
    """HTTP 상태 코드를 보존하여 `app.agents.connection.classify` 가 실패 갈래를 정확히 분류하게 합니다."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code


@dataclass(frozen=True)
class DecisionQuestion:
    """`/v1/systemone` 의 단일 질문 명세.

    `criteria` 는 질문 종류에 따라 뜻이 다릅니다.

    - `score`: 낮은 단계부터 적은 단계 이름 목록. 목록 그대로 보냅니다.
    - `choice`: 선택지 이름. 답의 `choice` 와 `probabilities` 키가 이 이름으로 돌아옵니다.
      Jev 는 choice 의 criteria 를 `{이름: 설명}` 사전으로만 받고, 목록을 보내면 422 로 거절합니다.
      설명은 `choice_notes` 에 같은 순서로 적고, 비워 두면 이름을 그대로 설명으로 씁니다.
    """

    type: QuestionKind
    instructions: str
    criteria: Optional[Tuple[str, ...]] = None
    choice_notes: Optional[Tuple[str, ...]] = None

    def to_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "type": self.type,
            "instructions": self.instructions,
        }
        if not self.criteria:
            return payload
        if self.type == "choice":
            notes = self.choice_notes or ()
            payload["criteria"] = {
                name: (notes[i] if i < len(notes) and notes[i] else name)
                for i, name in enumerate(self.criteria)
            }
        elif self.type == "score":
            payload["criteria"] = list(self.criteria)
        return payload


@dataclass(frozen=True)
class DecisionAnswer:
    """정규화된 단일 질문 판정 결과."""

    type: str
    noul: Optional[float] = None
    choice: Optional[str] = None
    score: Optional[float] = None
    legend: Tuple[str, ...] = ()
    probabilities: Dict[str, float] = field(default_factory=dict)
    confidence: Optional[float] = None


@dataclass(frozen=True)
class DecisionResponse:
    """`/v1/systemone` 또는 `laya.Router.predict` 의 정규화된 응답."""

    model: str
    answers: Dict[str, DecisionAnswer]
    input_tokens: int = 0
    output_tokens: int = 0


def _decision_model_label(cfg: DecisionLLMConfig) -> str:
    """실패 안내에 적을 결정 모델 이름 (`Agent.display_model` 과 같은 `백엔드/모델` 모양)."""
    return f"{cfg.backend}/{cfg.effective_model}"


def build_systemone_urls(api_base: str, backend: str) -> Tuple[str, Optional[str]]:
    """`api_base` 로부터 주 호출 URL(`/v1/systemone`)과 Laya 전용 폴백 URL(`/predict`)을 만듭니다."""
    base = api_base.strip().rstrip("/")
    if base.endswith("/systemone") or base.endswith("/predict"):
        return base, None
    if base.endswith("/v1"):
        primary = f"{base}/systemone"
        fallback = f"{base[:-3]}/predict" if backend == "laya" else None
        return primary, fallback
    primary = f"{base}/v1/systemone"
    fallback = f"{base}/predict" if backend == "laya" else None
    return primary, fallback


def _clamp_prob(value: Any, default: float = 0.0) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, num))


def _parse_answer(q_spec: Optional[DecisionQuestion], raw: Any) -> DecisionAnswer:
    """Jev / Laya 의 다양한 응답 표현(사전·숫자·문자열·객체)을 `DecisionAnswer` 로 정규화합니다."""
    expected_type = q_spec.type if q_spec is not None else "noul"
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        if expected_type == "score":
            return DecisionAnswer(type="score", score=float(raw), legend=q_spec.criteria if q_spec and q_spec.criteria else ())
        return DecisionAnswer(type="noul", noul=_clamp_prob(raw))
    if isinstance(raw, str):
        return DecisionAnswer(type="choice", choice=raw)

    if not isinstance(raw, Mapping):
        # 객체 속성으로 내려오는 로컬 라우터 응답 지원
        raw_dict: Dict[str, Any] = {
            k: getattr(raw, k)
            for k in ("type", "noul", "probability", "choice", "score", "legend", "probabilities", "confidence")
            if hasattr(raw, k)
        }
    else:
        raw_dict = dict(raw)

    kind = str(raw_dict.get("type") or expected_type)
    probs_raw = raw_dict.get("probabilities")
    probs: Dict[str, float] = {}
    if isinstance(probs_raw, Mapping):
        for k, v in probs_raw.items():
            try:
                probs[str(k)] = float(v)
            except (TypeError, ValueError):
                continue

    conf_raw = raw_dict.get("confidence")
    confidence: Optional[float] = None
    if conf_raw is not None:
        try:
            confidence = _clamp_prob(conf_raw)
        except (TypeError, ValueError):
            confidence = None

    noul_val: Optional[float] = None
    if kind == "noul" or "noul" in raw_dict or "probability" in raw_dict:
        raw_p = raw_dict.get("noul", raw_dict.get("probability"))
        if raw_p is None and probs:
            raw_p = probs.get("yes", probs.get("true", probs.get("1")))
        noul_val = _clamp_prob(raw_p, default=0.0)

    choice_val: Optional[str] = None
    if raw_dict.get("choice") is not None:
        choice_val = str(raw_dict["choice"])
    elif kind == "choice" and probs:
        choice_val = max(probs.items(), key=lambda kv: kv[1])[0]

    score_val: Optional[float] = None
    if raw_dict.get("score") is not None:
        try:
            score_val = float(raw_dict["score"])
        except (TypeError, ValueError):
            score_val = None

    legend_raw = raw_dict.get("legend")
    if isinstance(legend_raw, (list, tuple)):
        legend = tuple(str(x) for x in legend_raw)
    elif q_spec and q_spec.criteria:
        legend = tuple(q_spec.criteria)
    else:
        legend = ()

    return DecisionAnswer(
        type=kind,
        noul=noul_val,
        choice=choice_val,
        score=score_val,
        legend=legend,
        probabilities=probs,
        confidence=confidence,
    )


def _normalize_response(
    raw: Any,
    default_model: str,
    questions: Mapping[str, DecisionQuestion],
) -> DecisionResponse:
    if not isinstance(raw, Mapping):
        raw = {
            k: getattr(raw, k)
            for k in ("model", "answers", "usage")
            if hasattr(raw, k)
        } or {"answers": raw}

    answers_raw = raw.get("answers")
    if not isinstance(answers_raw, Mapping):
        # 일부 Laya 로컬 버전은 최상위에 바로 {qid: answer} 사전을 돌려줍니다.
        if isinstance(raw, Mapping) and any(qid in raw for qid in questions):
            answers_raw = {qid: raw[qid] for qid in questions if qid in raw}
        else:
            answers_raw = {}

    answers: Dict[str, DecisionAnswer] = {}
    for qid, q_spec in questions.items():
        if qid in answers_raw:
            answers[qid] = _parse_answer(q_spec, answers_raw[qid])

    usage_raw = raw.get("usage")
    in_tok, out_tok = 0, 0
    if isinstance(usage_raw, Mapping):
        try:
            in_tok = max(0, int(usage_raw.get("input_tokens") or usage_raw.get("prompt_tokens") or 0))
        except (TypeError, ValueError):
            in_tok = 0
        try:
            out_tok = max(0, int(usage_raw.get("output_tokens") or usage_raw.get("completion_tokens") or 0))
        except (TypeError, ValueError):
            out_tok = 0

    model_name = str(raw.get("model") or default_model)
    return DecisionResponse(
        model=model_name,
        answers=answers,
        input_tokens=in_tok,
        output_tokens=out_tok,
    )


def _compile_messages_to_state(messages: Sequence[Mapping[str, Any]], max_chars: int = MAX_DECISION_STATE_CHARS) -> str:
    """OpenAI 형식의 메시지 배열을 System 1 `state` 단일 텍스트로 직렬화합니다."""
    parts: List[str] = []
    for msg in messages:
        role = str(msg.get("role") or "user").upper()
        content = msg.get("content")
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            chunks = [
                str(block.get("text") or "")
                for block in content
                if isinstance(block, Mapping) and block.get("type") == "text"
            ]
            text = "\n".join(c for c in chunks if c.strip()).strip()
        else:
            text = str(content or "").strip()
        if text:
            parts.append(f"[{role}]\n{text}")
    joined = "\n\n".join(parts)
    if len(joined) <= max_chars:
        return joined
    # 앞쪽 시스템·목표 블록과 뒤쪽 최신 지시 블록을 모두 보존합니다.
    head_budget = max_chars // 2
    tail_budget = max_chars - head_budget - 40
    return f"{joined[:head_budget]}\n\n...(중간 맥락 생략)...\n\n{joined[-tail_budget:]}"


class DecisionCaller:
    """Jev 및 Laya System 1 결정 모델을 호출하고 MADO 오케스트레이션·전문가 판정을 수행합니다."""

    def __init__(self, http_transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
        self._http_transport = http_transport
        self._laya_routers: Dict[Tuple[str, str], Any] = {}

    async def evaluate(
        self,
        cfg: DecisionLLMConfig,
        state_text: str,
        questions: Mapping[str, DecisionQuestion],
        *,
        agent: Optional[Agent] = None,
        record_usage: bool = True,
    ) -> DecisionResponse:
        """단일 포워드 패스로 `questions` 전체를 평가합니다."""
        if not cfg.is_live:
            reason = (
                f"결정 LLM({cfg.backend.upper()}) 연결 정보가 설정되어 있지 않습니다. "
                f".env 의 {'JEV_API_KEY' if cfg.backend == 'jev' else 'LAYA_API_KEY / LAYA_API_BASE'} "
                f"또는 conf.json 의 decision_llm 설정을 확인하세요."
            )
            if agent is not None:
                raise LLMUnavailableError(agent, reason, model=_decision_model_label(cfg), endpoint=cfg.endpoint_label)
            raise RuntimeError(reason)

        try:
            async with get_llm_gate().slot():
                began = time.monotonic()
                try:
                    response = await self._evaluate_unthrottled(cfg, state_text, questions)
                finally:
                    if record_usage:
                        llm_usage.note_call(time.monotonic() - began)
        except LLMUnavailableError:
            raise
        except Exception as exc:
            if agent is not None:
                logger.error(
                    "Decision LLM call failed for %s (backend=%s, model=%s, endpoint=%s): %s",
                    agent.name, cfg.backend, cfg.effective_model, cfg.endpoint_label, exc,
                )
                raise LLMUnavailableError(
                    agent,
                    f"{type(exc).__name__}: {exc}",
                    model=_decision_model_label(cfg),
                    endpoint=cfg.endpoint_label,
                ) from exc
            raise

        if record_usage and (response.input_tokens or response.output_tokens):
            llm_usage.note_response({
                "usage": {
                    "prompt_tokens": response.input_tokens,
                    "completion_tokens": response.output_tokens,
                }
            })
        return response

    async def _evaluate_unthrottled(
        self,
        cfg: DecisionLLMConfig,
        state_text: str,
        questions: Mapping[str, DecisionQuestion],
    ) -> DecisionResponse:
        api_base = cfg.effective_api_base
        if cfg.backend == "laya" and not api_base:
            return await self._call_laya_local(cfg, state_text, questions)
        if not api_base:
            raise RuntimeError(f"결정 LLM({cfg.backend}) HTTP 엔드포인트 주소가 없습니다.")
        return await self._call_http(cfg, api_base, state_text, questions)

    async def _call_http(
        self,
        cfg: DecisionLLMConfig,
        api_base: str,
        state_text: str,
        questions: Mapping[str, DecisionQuestion],
    ) -> DecisionResponse:
        primary_url, fallback_url = build_systemone_urls(api_base, cfg.backend)
        model_name = cfg.effective_model
        payload = {
            "model": model_name,
            "state": state_text,
            "questions": {qid: q.to_payload() for qid, q in questions.items()},
        }
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        api_key = cfg.effective_api_key
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        async with httpx.AsyncClient(timeout=cfg.timeout, transport=self._http_transport) as client:
            resp = await client.post(primary_url, json=payload, headers=headers)
            if resp.status_code == 404 and fallback_url:
                resp = await client.post(fallback_url, json=payload, headers=headers)

        if resp.status_code >= 400:
            body = resp.text.strip()
            try:
                err_json = resp.json()
                if isinstance(err_json, Mapping):
                    err_obj = err_json.get("error") or err_json.get("detail") or err_json.get("message")
                    if isinstance(err_obj, Mapping):
                        body = str(err_obj.get("message") or body)
                    elif err_obj:
                        body = str(err_obj)
            except Exception:  # noqa: BLE001
                pass
            raise DecisionHTTPStatusError(resp.status_code, body or resp.reason_phrase)

        data = resp.json()
        return _normalize_response(data, model_name, questions)

    async def _call_laya_local(
        self,
        cfg: DecisionLLMConfig,
        state_text: str,
        questions: Mapping[str, DecisionQuestion],
    ) -> DecisionResponse:
        model_path = cfg.effective_model_path or ""
        model_name = cfg.effective_model
        cache_key = (model_path, model_name)
        payload_questions = {qid: q.to_payload() for qid, q in questions.items()}

        def _run_sync() -> Any:
            router = self._laya_routers.get(cache_key)
            if router is None:
                laya_mod = importlib.import_module("laya")
                router_cls = getattr(laya_mod, "Router", None)
                if router_cls is None:
                    raise RuntimeError("설치된 laya 패키지에서 Router 클래스를 찾지 못했습니다.")
                router = router_cls(model_path) if model_path else router_cls()
                self._laya_routers[cache_key] = router
            return router.predict(state_text, payload_questions)

        raw = await asyncio.to_thread(_run_sync)
        return _normalize_response(raw, model_name, questions)

    async def probe(self, cfg: DecisionLLMConfig, timeout_seconds: float = 15.0) -> None:
        """`model_status` 연결 확인용 초경량 단일 질문 호출."""
        probe_cfg = cfg.model_copy(update={"timeout": timeout_seconds})
        await self._evaluate_unthrottled(
            probe_cfg,
            "ping",
            {"ping": DecisionQuestion(type="noul", instructions="Respond ready?")},
        )

    # ------------------------------------------------------------ 오케스트레이터 제어면 판정

    async def evaluate_gate(
        self,
        orchestrator: Agent,
        state_text: str,
        question: str,
    ) -> Tuple[str, str]:
        """그래프 `gate` 노드의 예/아니오 분기를 단일 `noul` 패스로 판정합니다."""
        cfg = orchestrator.decision_llm
        q = DecisionQuestion(type="noul", instructions=question.strip())
        resp = await self.evaluate(cfg, state_text, {"gate": q}, agent=orchestrator)
        ans = resp.answers.get("gate")
        prob = ans.noul if (ans is not None and ans.noul is not None) else 0.0
        decision = "yes" if prob >= cfg.threshold else "no"
        conf_note = f", 신뢰도 {ans.confidence:.2f}" if (ans and ans.confidence is not None) else ""
        reason = (
            f"System 1({cfg.backend.upper()}/{resp.model}) 확률 판정 "
            f"p={prob:.2f} (기준 {cfg.threshold:.2f}{conf_note})"
        )
        return decision, reason

    async def select_speakers(
        self,
        orchestrator: Agent,
        state_text: str,
        candidates: Sequence[Agent],
        round_num: int,
        max_rounds: int,
    ) -> Tuple[List[Agent], str]:
        """`orchestrator_led` 전략에서 후보 전원의 발언 적합도를 한 번의 패스로 평가해 지명합니다."""
        if not candidates:
            return [], ""
        cfg = orchestrator.decision_llm
        questions: Dict[str, DecisionQuestion] = {
            cand.key: DecisionQuestion(
                type="noul",
                instructions=(
                    f"Round {round_num}/{max_rounds} 논의를 진전시키기 위해 "
                    f"'{cand.name}' ({cand.role}) 전문가가 이번 라운드에 발언해야 합니까?"
                ),
            )
            for cand in candidates
        }
        resp = await self.evaluate(cfg, state_text, questions, agent=orchestrator)

        scored: List[Tuple[float, int, Agent]] = []
        for idx, cand in enumerate(candidates):
            ans = resp.answers.get(cand.key)
            prob = ans.noul if (ans is not None and ans.noul is not None) else 0.0
            scored.append((prob, idx, cand))

        passing = [item for item in scored if item[0] >= cfg.threshold]
        if passing:
            passing.sort(key=lambda item: (-item[0], item[1]))
            picked = [item[2] for item in passing]
        else:
            # 아무도 임계치를 넘지 못하면 가장 확률이 높은 후보 1명을 지명해 라운드 공전을 막습니다.
            best = max(scored, key=lambda item: (item[0], -item[1]))
            picked = [best[2]]

        score_map = {cand.key: prob for prob, _idx, cand in scored}
        picked_desc = ", ".join(f"{a.name}({score_map[a.key]:.2f})" for a in picked)
        reason = (
            f"System 1({cfg.backend.upper()}/{resp.model}) 발언 적합도 판정: "
            f"{picked_desc} (기준 {cfg.threshold:.2f})"
        )
        return picked, reason

    async def evaluate_sub_debate(
        self,
        orchestrator: Agent,
        state_text: str,
        issues: Sequence[str],
        candidates: Sequence[Agent],
    ) -> Optional[Tuple[List[Agent], str, str, Optional[int]]]:
        """라운드 경계에서 서브 토론 개시 여부, 핵심 쟁점, 당사자 2인을 단일 패스로 결정합니다.

        반환값은 (선정된 두 전문가, 쟁점 본문, 선정 사유, 쟁점 번호) 튜플입니다. 장부의 미해결 쟁점은 `[I-3] 내용` 형태이므로(ADR-051),
        선정된 항목에서 식별 태그를 분리하여 번호와 순수 본문 텍스트를 각각 반환합니다.
        """
        if len(candidates) < 2 or not issues:
            return None
        cfg = orchestrator.decision_llm
        questions: Dict[str, DecisionQuestion] = {
            "open": DecisionQuestion(
                type="noul",
                instructions=(
                    "결정 장부의 미해결 쟁점이 두 전문가의 의견 충돌에서 비롯되어, "
                    "본 토론과 분리된 집중 서브 토론을 지금 바로 열어야 합니까?"
                ),
            ),
        }
        issue_labels = [str(i) for i in range(len(issues))]
        if len(issues) > 1:
            questions["issue_idx"] = DecisionQuestion(
                type="choice",
                instructions="가장 시급히 두 전문가가 맞대어 해결해야 할 미해결 쟁점 번호를 고르세요.",
                criteria=tuple(issue_labels),
                # 번호만 보내면 모델이 번호와 쟁점을 짝지을 수 없으므로 쟁점 본문을 설명으로 붙입니다.
                choice_notes=tuple(issue.strip()[:MAX_CHOICE_NOTE_CHARS] for issue in issues),
            )
        for cand in candidates:
            questions[f"cand_{cand.key}"] = DecisionQuestion(
                type="noul",
                instructions=f"이 미해결 쟁점을 직접 조율할 당사자로 '{cand.name}' ({cand.role}) 전문가가 적합합니까?",
            )

        resp = await self.evaluate(cfg, state_text, questions, agent=orchestrator)
        open_ans = resp.answers.get("open")
        open_prob = open_ans.noul if (open_ans is not None and open_ans.noul is not None) else 0.0
        if open_prob < cfg.threshold:
            return None

        chosen_issue = issues[0]
        if len(issues) > 1:
            idx_ans = resp.answers.get("issue_idx")
            if idx_ans and idx_ans.choice in issue_labels:
                chosen_issue = issues[int(idx_ans.choice)]

        scored_cands: List[Tuple[float, int, Agent]] = []
        for idx, cand in enumerate(candidates):
            c_ans = resp.answers.get(f"cand_{cand.key}")
            c_prob = c_ans.noul if (c_ans is not None and c_ans.noul is not None) else 0.0
            scored_cands.append((c_prob, idx, cand))
        scored_cands.sort(key=lambda item: (-item[0], item[1]))
        pair = [scored_cands[0][2], scored_cands[1][2]]
        reason = (
            f"System 1({cfg.backend.upper()}/{resp.model}) 서브 토론 개시 판정 p={open_prob:.2f} · "
            f"당사자 적합도 {pair[0].name}({scored_cands[0][0]:.2f}), {pair[1].name}({scored_cands[1][0]:.2f})"
        )
        tag = _ISSUE_TAG.match(chosen_issue.strip())
        issue_no = int(tag.group(1)) if tag else None
        issue_text = chosen_issue.strip()[tag.end():].strip() if tag else chosen_issue.strip()
        return pair, issue_text, reason, issue_no

    # ------------------------------------------------------------ 전문가 System 1 발언 평가

    async def evaluate_specialist_turn(
        self,
        agent: Agent,
        formatted_messages: Sequence[Mapping[str, Any]],
        *,
        on_chunk: Optional[Callable[[str], Any]] = None,
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """`decision_llm.enabled` 가 켜진 전문가 에이전트의 단일 패스 구조화 판정 발언을 생성합니다."""
        cfg = agent.decision_llm
        state_text = _compile_messages_to_state(formatted_messages)
        role_context = f"{agent.name} ({agent.role})"

        quality_rubric = (
            "치명적 결함·재설계 필요",
            "보완 필요·위험 존재",
            "대체로 타당·부분 개선",
            "우수·즉시 채택 가능",
        )
        # 선택지 이름은 짧은 영문 키로 보내고 화면에는 설명(한국어)을 씁니다. Jev 문서의 예시가
        # 이 모양이고, 답의 `choice`·`probabilities` 는 키로 돌아옵니다.
        action_choices: Dict[str, str] = {
            "approve": "승인 및 합의 진행",
            "revise": "구체적 보완 후 진행",
            "reexamine": "핵심 쟁점 재검토 필요",
            "alternative": "대안 설계 채택 권고",
        }
        questions: Dict[str, DecisionQuestion] = {
            "approval": DecisionQuestion(
                type="noul",
                instructions=(
                    f"{role_context} 관점에서 현재 제안·설계·구현안이 사용자의 요구사항과 "
                    f"전문 품질 기준을 충족합니까?"
                ),
            ),
            "quality": DecisionQuestion(
                type="score",
                instructions=f"{role_context} 기준에서 현재 논의된 안의 완성도와 안전성을 평가하세요.",
                criteria=quality_rubric,
            ),
            "action": DecisionQuestion(
                type="choice",
                instructions=f"{role_context} 로서 다음 단계에 권고할 조치를 선택하세요.",
                criteria=tuple(action_choices),
                choice_notes=tuple(action_choices.values()),
            ),
        }

        resp = await self.evaluate(cfg, state_text, questions, agent=agent)
        app_ans = resp.answers.get("approval")
        qual_ans = resp.answers.get("quality")
        act_ans = resp.answers.get("action")

        prob = app_ans.noul if (app_ans and app_ans.noul is not None) else 0.0
        passed = prob >= cfg.threshold
        verdict_badge = "충족 (승인)" if passed else "미충족 (보완·재검토 필요)"

        max_idx = len(quality_rubric) - 1
        raw_score = qual_ans.score if (qual_ans and qual_ans.score is not None) else (prob * max_idx)
        clamped_idx = max(0, min(max_idx, int(round(raw_score))))
        quality_label = quality_rubric[clamped_idx]

        recommended = (
            action_choices.get(act_ans.choice, act_ans.choice)
            if (act_ans and act_ans.choice)
            else action_choices["approve" if passed else "revise"]
        )
        conf_parts: List[str] = []
        if qual_ans and qual_ans.confidence is not None:
            conf_parts.append(f"척도 신뢰도 {qual_ans.confidence:.2f}")
        if act_ans and act_ans.confidence is not None:
            conf_parts.append(f"선택 신뢰도 {act_ans.confidence:.2f}")
        conf_suffix = f" ({' · '.join(conf_parts)})" if conf_parts else ""

        lines = [
            f"### [{agent.name}] System 1 결정 모델 평가 (`{cfg.backend.upper()} · {resp.model}`)",
            "",
            f"- **타당성 판정**: **{verdict_badge}** — 충족 확률 `{prob:.1%}` (임계 기준 `{cfg.threshold:.0%}`)",
            f"- **품질 척도 점수**: **{raw_score:.2f} / {max_idx:.1f}** — *{quality_label}*{conf_suffix}",
            f"- **권고 조치**: **{recommended}**",
        ]
        if act_ans and act_ans.probabilities:
            dist = ", ".join(
                f"{action_choices.get(key, key)} {p:.0%}"
                for key, p in sorted(act_ans.probabilities.items(), key=lambda kv: -kv[1])
            )
            lines.append(f"- **선택지 확률 분포**: {dist}")

        lines.extend([
            "",
            "## 요지",
            (
                f"{agent.name}({agent.role})의 System 1(`{cfg.backend}/{resp.model}`) 단일 패스 판정 결과: "
                f"**{verdict_badge}** (충족 확률 {prob:.1%}, 척도 {raw_score:.2f}/{max_idx}: {quality_label}), "
                f"권고 조치는 **{recommended}** 입니다."
            ),
        ])
        report = "\n".join(lines)

        if on_chunk is not None:
            if inspect.iscoroutinefunction(on_chunk):
                await on_chunk(report)
            else:
                on_chunk(report)
        return report, []
