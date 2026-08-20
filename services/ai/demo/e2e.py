"""End-to-end: real resume, real posting, real Gemini, real guardrails."""

import asyncio
import json
import pathlib

from langgraph.checkpoint.memory import InMemorySaver

from app.agents.data_retriever import data_retriever_agent
from app.agents.evaluator import evaluator_agent
from app.agents.refactorer import refactorer_agent
from app.agents.scraper_keyword import scraper_keyword_agent
from app.compile.latex import compile_latex
from app.graph.builder import (
    DATA_RETRIEVER,
    EVALUATOR,
    HUMAN_REVIEW,
    REFACTORER,
    SCRAPER_KEYWORD,
    build_graph,
)
from app.graph.state import initial_state

F = pathlib.Path("tests/fixtures")
profile = json.loads((F / "real_profile.json").read_text())
resume = (F / "real_resume.tex").read_text()
jd = (F / "sample_jd.txt").read_text()


async def main():
    graph = build_graph(
        {
            SCRAPER_KEYWORD: scraper_keyword_agent,
            DATA_RETRIEVER: data_retriever_agent,
            REFACTORER: refactorer_agent,
            EVALUATOR: evaluator_agent,
        },
        checkpointer=InMemorySaver(),
        interrupt_before=(HUMAN_REVIEW,),
    )

    cfg = {"configurable": {"thread_id": "demo-e2e"}}
    state = initial_state(
        session_id="demo-e2e",
        user_id="u-aditya",
        user_latex=resume,
        user_profile=profile,
        job_text=jd,
    )

    out = await graph.ainvoke(state, config=cfg)
    print(f"paused at keyword confirmation: {(await graph.aget_state(cfg)).next}")
    print(f"  keywords extracted : {len(out['keywords'])} (0 LLM tokens)")

    out = await graph.ainvoke(None, config=cfg)  # user confirms keywords
    snap = await graph.aget_state(cfg)

    print(f"\npaused at            : {snap.next}")
    print(f"  evidence matched   : {len(out['matched_evidence'])} profile items")
    print(f"  gaps reported      : {len(out['unsupported_keywords'])}")
    print(f"  refactor attempts  : {out['iteration_count']}")
    ev = out.get("evaluation") or {}
    print(f"  evaluation passed  : {ev.get('passed')}")
    print(f"  factual errors     : {len(ev.get('factual_errors') or [])}")
    print(f"  structural errors  : {len(ev.get('structural_errors') or [])}")
    print(f"  quality issues     : {len(ev.get('quality_issues') or [])}")
    print(f"  keyword coverage   : {ev.get('keyword_coverage')}")
    led = out.get("token_ledger") or {}
    print(
        f"  TOKENS: {led.get('input_tokens')} in / {led.get('output_tokens')} out "
        f"over {led.get('calls')} call(s)"
    )
    for e in led.get("by_step") or []:
        print(
            f"     {e['step']:<22} {e['model']:<22} in={e['input_tokens']} "
            f"out={e['output_tokens']} think={e['thinking_tokens']}"
        )

    if out.get("error"):
        print(f"\nERROR: {out['error']}")
        return

    latex = out["refactored_latex"]
    print(f"\n  changelog entries  : {len(out.get('changelog') or [])}")
    for c in (out.get("changelog") or [])[:4]:
        print(f"     [{c.get('section')}] {c.get('change_type')}: {str(c.get('reason'))[:80]}")

    r = await compile_latex(latex, name="tailored", output_dir="/out")
    print(f"\n  PDF: ok={r.ok} bytes={r.pdf_bytes:,} pages={r.pages} ms={r.duration_ms:.0f}")
    if not r.ok:
        print(f"  compile error: {r.error}")
    pathlib.Path("/out/tailored.tex").write_text(latex)
    for w in out.get("warnings") or []:
        print(f"  warning: {w[:110]}")


asyncio.run(main())
