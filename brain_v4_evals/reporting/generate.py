"""生成离线可打开的旧链 / V4 双栏静态评测台。

输出结构：
- `<output>/index.html`：日期 / feature / 版本筛选与 comparison 索引；
- `<output>/<run-date>/<feature-id>.html`：双栏对比详情；
- `<output>/<run-date>/data/<comparison-id>.json`：不可变评测信封数据；
- `<output>/static/{index.js,styles.css}`：纯静态资源。

页面只内联数据与相对路径资源，不发任何实时请求，也不含研究逻辑。
"""

import html
import json
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..contracts import EvaluationEnvelope

_STATIC_DIR = Path(__file__).resolve().parent / "static"


@dataclass(frozen=True)
class ReportPaths:
    """一次生成的产物路径。"""

    index: Path
    pages: dict[str, Path]
    data: dict[str, Path]


def _safe_name(value: str) -> str:
    """只保留文件名安全字符，防止 feature id 造成路径穿越。"""
    cleaned = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in value)
    return cleaned or "feature"


def _escape(value: object) -> str:
    return html.escape(str(value))


def _json_block(payload: object) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return text.replace("</", "<\\/")


def _render_output(output: dict[str, Any]) -> str:
    return _escape(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))


def _list_items(values: Sequence[str]) -> str:
    if not values:
        return "<p class=\"empty\">无</p>"
    return "<ul>" + "".join(f"<li>{_escape(value)}</li>" for value in values) + "</ul>"


def _render_comparison(envelope: EvaluationEnvelope) -> str:
    hypotheses = "".join(
        "<li>"
        f"<strong>{_escape(item.hypothesis_ref)}</strong> "
        f"{_escape(item.statement)} "
        f"<span class=\"tag\">{_escape(item.status)}</span> "
        f"<span class=\"score\">{item.confidence:.2f}</span>"
        "</li>"
        for item in envelope.hypotheses
    )
    if not hypotheses:
        hypotheses = "<li class=\"empty\">无</li>"

    revisions = "".join(
        "<li>"
        f"<strong>{_escape(item.revision_id)}</strong> "
        f"{_escape(item.action)} · {_escape(item.reason)}"
        "</li>"
        for item in envelope.revisions
    )
    if not revisions:
        revisions = "<li class=\"empty\">无</li>"

    status = str(envelope.v4_output.get("status", "ok"))
    reviewer = "未评分" if envelope.reviewer_score is None else f"{envelope.reviewer_score:.2f}"
    token_usage = envelope.token_usage

    return f"""
<section class="comparison-card" id="{_escape(envelope.comparison_id)}">
  <header class="comparison-header">
    <h2>comparison {_escape(envelope.comparison_id)}</h2>
    <span class="tag">盲标 {_escape(envelope.blind_label)}</span>
    <span class="tag">评分 {_escape(reviewer)}</span>
    <span class="tag">降级状态 {_escape(status)}</span>
  </header>
  <table class="meta">
    <tbody>
      <tr><th>snapshot id</th><td>{_escape(envelope.snapshot_id)}</td></tr>
      <tr><th>snapshot sha</th><td class="sha">{_escape(envelope.snapshot_sha)}</td></tr>
      <tr><th>as_of</th><td>{_escape(envelope.as_of.isoformat())}</td></tr>
      <tr><th>data_cutoff</th><td>{_escape(envelope.data_cutoff.isoformat())}</td></tr>
      <tr><th>model / config / method</th><td>{_escape(envelope.model_version)} / {_escape(envelope.config_version)} / {_escape(envelope.method_version)}</td></tr>
      <tr><th>legacy / v4 run</th><td>{_escape(envelope.legacy_run_id)} / {_escape(envelope.v4_run_id)}</td></tr>
    </tbody>
  </table>
  <div class="comparison-grid">
    <article class="column legacy">
      <h3>旧链 Legacy</h3>
      <pre>{_render_output(envelope.legacy_output)}</pre>
    </article>
    <article class="column v4">
      <h3>V4</h3>
      <pre>{_render_output(envelope.v4_output)}</pre>
      <p class="evidence-summary">{_escape(envelope.evidence_summary.summary)}</p>
    </article>
  </div>
  <div class="detail-grid">
    <section>
      <h3>证据</h3>
      {_list_items(envelope.evidence_summary.evidence_ids)}
    </section>
    <section>
      <h3>反证 / 反例</h3>
      {_list_items(envelope.evidence_summary.counter_evidence_ids)}
    </section>
    <section>
      <h3>假设</h3>
      <ul>{hypotheses}</ul>
    </section>
    <section>
      <h3>回改</h3>
      <ul>{revisions}</ul>
    </section>
    <section>
      <h3>未解决问题</h3>
      {_list_items(envelope.unresolved_questions)}
    </section>
    <section>
      <h3>性能与成本</h3>
      <ul>
        <li>latency {envelope.latency_ms} ms</li>
        <li>tokens {token_usage.total_tokens}（prompt {token_usage.prompt_tokens} / completion {token_usage.completion_tokens}）</li>
        <li>cost {envelope.cost:.4f}</li>
      </ul>
    </section>
  </div>
</section>
"""


def _render_feature_page(feature_id: str, run_date: str, envelopes: Sequence[EvaluationEnvelope]) -> str:
    sections = "\n".join(_render_comparison(envelope) for envelope in envelopes)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Brain V4 评测 · {_escape(feature_id)} · {_escape(run_date)}</title>
  <link rel="stylesheet" href="../static/styles.css">
</head>
<body>
  <nav class="back"><a href="../index.html">返回索引</a></nav>
  <h1>{_escape(feature_id)} · {_escape(run_date)}</h1>
  <p class="hint">旧链不是标准答案；盲标用于人工质量比较，评测台不含研究逻辑。</p>
  {sections}
  <script src="../static/index.js"></script>
</body>
</html>
"""


def _render_index(
    run_date: str,
    items: Sequence[dict[str, Any]],
) -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Brain V4 评测台</title>
  <link rel="stylesheet" href="static/styles.css">
</head>
<body>
  <h1>Brain V4 评测台 · {_escape(run_date)}</h1>
  <section class="filters">
    <label>日期 <select id="filter-date"><option value="">全部</option></select></label>
    <label>Feature <select id="filter-feature"><option value="">全部</option></select></label>
    <label>版本 <input id="filter-version" type="text" placeholder="method_version"></label>
  </section>
  <table class="index-table">
    <thead>
      <tr><th>comparison</th><th>feature</th><th>profile</th><th>blind</th><th>snapshot sha</th><th>版本</th></tr>
    </thead>
    <tbody id="comparison-rows"></tbody>
  </table>
  <script id="report-data" type="application/json">{_json_block(list(items))}</script>
  <script src="static/index.js"></script>
</body>
</html>
"""


def generate_report(
    envelopes: Sequence[EvaluationEnvelope],
    output_dir: str | Path,
) -> ReportPaths:
    """把评测信封写成离线静态站点；返回 index、页面与 JSON 数据路径。"""
    root = Path(output_dir)
    items = sorted(envelopes, key=lambda env: (env.as_of, env.feature_id, env.comparison_id))
    if not items:
        raise ValueError("generate_report 需要至少一个 EvaluationEnvelope")

    run_date = max(envelope.as_of for envelope in items).date().isoformat()
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(_STATIC_DIR, root / "static", dirs_exist_ok=True)

    run_dir = root / run_date
    data_dir = run_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    data_paths: dict[str, Path] = {}
    for envelope in items:
        path = data_dir / f"{_safe_name(envelope.comparison_id)}.json"
        path.write_text(
            json.dumps(envelope.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        data_paths[envelope.comparison_id] = path

    grouped: dict[str, list[EvaluationEnvelope]] = {}
    for envelope in items:
        grouped.setdefault(_safe_name(envelope.feature_id), []).append(envelope)

    pages: dict[str, Path] = {}
    index_items: list[dict[str, Any]] = []
    for feature_name, feature_items in sorted(grouped.items()):
        page_relative = f"{run_date}/{feature_name}.html"
        page_path = root / page_relative
        page_path.write_text(
            _render_feature_page(feature_name, run_date, feature_items), encoding="utf-8"
        )
        pages[feature_name] = page_path
        for envelope in feature_items:
            index_items.append(
                {
                    "comparison_id": envelope.comparison_id,
                    "feature_id": envelope.feature_id,
                    "profile": envelope.profile,
                    "blind_label": envelope.blind_label,
                    "snapshot_sha": envelope.snapshot_sha,
                    "method_version": envelope.method_version,
                    "run_date": run_date,
                    "page": page_relative,
                }
            )

    index_path = root / "index.html"
    index_path.write_text(_render_index(run_date, index_items), encoding="utf-8")
    return ReportPaths(index=index_path, pages=pages, data=data_paths)
