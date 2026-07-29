"""Local-only FastAPI/Canvas annotation interface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from .annotations import (
    OCCURRENCE_RELATION_CODES,
    ROLE_CODES,
    annotation_progress,
    append_annotation_event,
    approve_pilot_item,
    set_annotation_item_decision,
)
from .workspace import Workspace, open_database


class AnnotationEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    annotator_id: str = Field(min_length=1, max_length=100)
    annotation_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    supersedes_event_id: str | None = None


class PilotApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    annotator_id: str = Field(min_length=1, max_length=100)


class ItemDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str


def create_app(workspace: Workspace) -> FastAPI:
    app = FastAPI(
        title="Lip Color Minimal Annotation Tool",
        version="0.2.0",
        docs_url="/api/docs",
        redoc_url=None,
    )

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _HTML

    @app.get("/api/sets")
    def list_sets(
        include_superseded: bool = Query(default=False),
    ) -> list[dict[str, Any]]:
        with open_database(workspace.database_path, readonly=True) as connection:
            where = "" if include_superseded else "WHERE status <> 'superseded'"
            return [
                {
                    "annotation_set_id": row["annotation_set_id"],
                    "name": row["name"],
                    "version": row["version"],
                    "purpose": row["purpose"],
                    "status": row["status"],
                }
                for row in connection.execute(
                    f"""
                    SELECT annotation_set_id, name, version, purpose, status
                    FROM annotation_sets {where} ORDER BY created_at
                    """
                )
            ]

    @app.get("/api/sets/{annotation_set_id}/items")
    def list_items(
        annotation_set_id: str,
        visibility: str = Query(default="image_only"),
        status: str | None = Query(default=None),
        sample_only: bool = Query(default=False),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        if visibility not in {"image_only", "occurrence_context"}:
            raise HTTPException(400, "invalid visibility")
        clauses = [
            "item.annotation_set_id = ?",
            "item.content_context_visibility = ?",
        ]
        params: list[Any] = [annotation_set_id, visibility]
        if status:
            clauses.append("item.status = ?")
            params.append(status)
        if sample_only:
            clauses.append(
                """
                EXISTS (
                    SELECT 1
                    FROM context_review_sample_items AS sample
                    JOIN context_review_sampling_policies AS policy
                      ON policy.context_review_sampling_policy_id =
                         sample.context_review_sampling_policy_id
                    WHERE sample.annotation_item_id =
                          item.annotation_item_id
                      AND policy.context_review_sampling_policy_id = (
                          SELECT latest.context_review_sampling_policy_id
                          FROM context_review_sampling_policies AS latest
                          WHERE latest.annotation_set_id =
                                item.annotation_set_id
                          ORDER BY latest.created_at DESC
                          LIMIT 1
                      )
                )
                """
            )
        params.append(limit)
        with open_database(workspace.database_path, readonly=True) as connection:
            return [
                {
                    "annotation_item_id": row["annotation_item_id"],
                    "task_types": json.loads(row["task_types_json"]),
                    "coverage_tags": json.loads(row["coverage_tags_json"]),
                    "split": row["split"],
                    "status": row["status"],
                }
                for row in connection.execute(
                    f"""
                    SELECT item.annotation_item_id, item.task_types_json,
                           item.coverage_tags_json, item.split, item.status
                    FROM annotation_items AS item
                    WHERE {' AND '.join(clauses)}
                    ORDER BY status, annotation_item_id
                    LIMIT ?
                    """,
                    params,
                )
            ]

    @app.get("/api/items/{annotation_item_id}")
    def get_item(
        annotation_item_id: str,
        annotator_id: str | None = Query(default=None, max_length=100),
    ) -> dict[str, Any]:
        with open_database(workspace.database_path, readonly=True) as connection:
            item = connection.execute(
                """
                SELECT item.annotation_item_id, item.task_types_json,
                       item.content_context_visibility,
                       item.coverage_tags_json, item.split, item.status,
                       item.global_thumbnail_asset_id,
                       annotation_set.purpose AS annotation_set_purpose,
                       (
                         SELECT sample.model_relationship
                         FROM context_review_sample_items AS sample
                         JOIN context_review_sampling_policies AS policy
                           ON policy.context_review_sampling_policy_id =
                              sample.context_review_sampling_policy_id
                         WHERE sample.annotation_item_id =
                               item.annotation_item_id
                         ORDER BY policy.created_at DESC
                         LIMIT 1
                       ) AS sample_model_relationship,
                       asset.width AS asset_width,
                       asset.height AS asset_height,
                       asset.metadata_json,
                       (
                         SELECT observation.width
                         FROM image_preprocessing_observations AS observation
                         WHERE observation.image_id = item.image_id
                           AND observation.decode_status = 'ok'
                         ORDER BY observation.created_at DESC
                         LIMIT 1
                       ) AS observation_width,
                       (
                         SELECT observation.height
                         FROM image_preprocessing_observations AS observation
                         WHERE observation.image_id = item.image_id
                           AND observation.decode_status = 'ok'
                         ORDER BY observation.created_at DESC
                         LIMIT 1
                       ) AS observation_height
                FROM annotation_items AS item
                JOIN annotation_sets AS annotation_set
                  ON annotation_set.annotation_set_id =
                     item.annotation_set_id
                LEFT JOIN derived_assets AS asset
                  ON asset.derived_asset_id = item.global_thumbnail_asset_id
                WHERE item.annotation_item_id = ?
                """,
                (annotation_item_id,),
            ).fetchone()
            if item is None:
                raise HTTPException(404, "annotation item not found")
            coverage_tags = json.loads(item["coverage_tags_json"])
            events = [
                {
                    "annotation_event_id": row["annotation_event_id"],
                    "annotator_id": row["annotator_id"],
                    "annotation_type": row["annotation_type"],
                    "after": json.loads(row["after_json"]),
                    "supersedes_event_id": row["supersedes_event_id"],
                    "created_at": row["created_at"],
                }
                for row in connection.execute(
                    """
                    SELECT annotation_event_id, annotator_id,
                           annotation_type, after_json,
                           supersedes_event_id, created_at
                    FROM annotation_events
                    WHERE annotation_item_id = ?
                    ORDER BY created_at, annotation_event_id
                    """,
                    (annotation_item_id,),
                )
            ]
            if "blind_review_required" in coverage_tags:
                events = [
                    event
                    for event in events
                    if event["annotation_type"]
                    not in {"role", "eligibility", "adjudication"}
                    or (
                        annotator_id is not None
                        and event["annotator_id"] == annotator_id
                    )
                ]
            asset_metadata = json.loads(item["metadata_json"] or "{}")
            oriented_size = asset_metadata.get("oriented_size")
            if (
                not isinstance(oriented_size, list)
                or len(oriented_size) != 2
            ):
                oriented_size = [
                    item["observation_width"] or item["asset_width"],
                    item["observation_height"] or item["asset_height"],
                ]
            # This payload is intentionally anonymous. No image/content ID,
            # source path, folder, SKU, source record, or shade context appears.
            payload: dict[str, Any] = {
                "annotation_item_id": item["annotation_item_id"],
                "task_types": json.loads(item["task_types_json"]),
                "content_context_visibility": item[
                    "content_context_visibility"
                ],
                "coverage_tags": coverage_tags,
                "split": item["split"],
                "status": item["status"],
                "annotation_set_purpose": item["annotation_set_purpose"],
                "context_review_sample_required": (
                    item["sample_model_relationship"] is not None
                ),
                "sample_model_relationship": item[
                    "sample_model_relationship"
                ],
                "asset_url": f"/api/items/{annotation_item_id}/asset",
                "oriented_width": oriented_size[0],
                "oriented_height": oriented_size[1],
                "events": events,
                "role_codes": ROLE_CODES,
                "occurrence_relation_codes": OCCURRENCE_RELATION_CODES,
            }
            if item["content_context_visibility"] == "occurrence_context":
                payload["occurrence_context_url"] = (
                    f"/api/items/{annotation_item_id}/occurrence-context"
                )
            return payload

    @app.get("/api/items/{annotation_item_id}/occurrence-context")
    def get_occurrence_context(annotation_item_id: str) -> dict[str, Any]:
        with open_database(workspace.database_path, readonly=True) as connection:
            item = connection.execute(
                """
                SELECT item.*, annotation_set.run_id
                FROM annotation_items AS item
                JOIN annotation_sets AS annotation_set
                  ON annotation_set.annotation_set_id =
                     item.annotation_set_id
                WHERE item.annotation_item_id = ?
                """,
                (annotation_item_id,),
            ).fetchone()
            if item is None:
                raise HTTPException(404, "annotation item not found")
            if item["content_context_visibility"] != "occurrence_context":
                raise HTTPException(
                    403, "source context is forbidden for this content task"
                )
            rows = connection.execute(
                """
                SELECT occurrence.brand_folder_raw,
                       occurrence.product_folder_raw,
                       record.sku_id_raw, record.sku_name_raw,
                       record.sku_concat_name_raw, record.sku_color_no_raw,
                       ref.source_field, ref.image_index,
                       fusion.occurrence_context_fusion_id,
                       fusion.relationship_to_context AS model_relationship,
                       fusion.confidence AS model_confidence,
                       fusion.context_conflicts_json AS model_conflicts_json
                FROM image_occurrences AS occurrence
                JOIN source_ref_occurrences AS link
                  ON link.image_occurrence_id =
                     occurrence.image_occurrence_id
                JOIN source_image_refs AS ref
                  ON ref.source_ref_id = link.source_ref_id
                JOIN source_records AS record
                  ON record.source_record_id = ref.source_record_id
                LEFT JOIN occurrence_context_fusions AS fusion
                  ON fusion.run_id = ?
                 AND fusion.image_occurrence_id =
                     occurrence.image_occurrence_id
                 AND fusion.source_record_id = record.source_record_id
                 AND fusion.source_ref_id = ref.source_ref_id
                WHERE occurrence.image_occurrence_id = ?
                ORDER BY record.source_record_id, ref.source_ref_id
                """,
                (item["run_id"], item["image_occurrence_id"]),
            )
            contexts: list[dict[str, Any]] = []
            for row in rows:
                context = dict(row)
                context["model_conflicts"] = json.loads(
                    context.pop("model_conflicts_json") or "[]"
                )
                contexts.append(context)
            return {
                "annotation_item_id": annotation_item_id,
                "contexts": contexts,
            }

    @app.get("/api/items/{annotation_item_id}/asset")
    def get_asset(annotation_item_id: str) -> FileResponse:
        with open_database(workspace.database_path, readonly=True) as connection:
            row = connection.execute(
                """
                SELECT asset.relative_path, asset.format
                FROM annotation_items AS item
                JOIN derived_assets AS asset
                  ON asset.derived_asset_id = item.global_thumbnail_asset_id
                WHERE item.annotation_item_id = ?
                """,
                (annotation_item_id,),
            ).fetchone()
        if row is None:
            raise HTTPException(404, "display asset not found")
        root = workspace.output_root.resolve()
        path = (root / row["relative_path"]).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise HTTPException(403, "asset path escaped output root") from exc
        if not path.is_file():
            raise HTTPException(404, "asset file not found")
        media_type = {
            "JPEG": "image/jpeg",
            "PNG": "image/png",
        }.get(str(row["format"]).upper(), "application/octet-stream")
        return FileResponse(path, media_type=media_type)

    @app.post("/api/items/{annotation_item_id}/events")
    def post_event(
        annotation_item_id: str,
        request: AnnotationEventRequest,
    ) -> dict[str, Any]:
        try:
            return append_annotation_event(
                workspace,
                annotation_item_id=annotation_item_id,
                annotator_id=request.annotator_id,
                annotation_type=request.annotation_type,
                payload=request.payload,
                supersedes_event_id=request.supersedes_event_id,
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/items/{annotation_item_id}/approve-pilot")
    def approve_pilot(
        annotation_item_id: str,
        request: PilotApprovalRequest,
    ) -> dict[str, str]:
        try:
            approve_pilot_item(
                workspace,
                annotation_item_id=annotation_item_id,
                annotator_id=request.annotator_id,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"status": "approved"}

    @app.post("/api/items/{annotation_item_id}/decision")
    def decide_item(
        annotation_item_id: str,
        request: ItemDecisionRequest,
    ) -> dict[str, str]:
        try:
            return set_annotation_item_decision(
                workspace,
                annotation_item_id=annotation_item_id,
                decision=request.decision,
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/sets/{annotation_set_id}/progress")
    def get_progress(annotation_set_id: str) -> dict[str, Any]:
        try:
            return annotation_progress(
                workspace, annotation_set_id=annotation_set_id
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    return app


_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>唇色图片最小标注工具</title>
  <style>
    :root { color-scheme: dark; font-family: system-ui, sans-serif; }
    body { margin: 0; background:#111318; color:#edf0f5; }
    header { padding:14px 20px; border-bottom:1px solid #343944; display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
    main { display:grid; grid-template-columns:minmax(420px,1fr) 390px; gap:18px; padding:18px; }
    .panel { background:#1a1e26; border:1px solid #343944; border-radius:10px; padding:14px; }
    canvas { width:100%; max-height:78vh; background:#0b0d10; cursor:crosshair; object-fit:contain; }
    label { display:block; margin:10px 0 4px; color:#aeb7c5; }
    select,input,textarea,button { width:100%; box-sizing:border-box; padding:9px; border-radius:6px; border:1px solid #485062; background:#11151c; color:#fff; }
    button { margin-top:9px; cursor:pointer; background:#3758d4; border-color:#5573e7; }
    button.secondary { background:#29303c; }
    .row { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
    .hidden { display:none !important; }
    .required { color:#ff8f8f; font-size:12px; }
    .optional { color:#98a2b3; font-size:12px; }
    .instructions { margin:10px 0; padding:10px 12px; border-left:3px solid #5573e7; background:#141923; line-height:1.55; font-size:13px; }
    pre { white-space:pre-wrap; max-height:180px; overflow:auto; font-size:12px; }
    .hint { font-size:12px; color:#98a2b3; }
    @media(max-width:900px){ main{grid-template-columns:1fr;} }
  </style>
</head>
<body>
<header>
  <strong>阶段 1.5 / 2.5 最小标注工具</strong>
  <select id="setSelect" style="width:330px"></select>
  <select id="visibility" style="width:190px">
    <option value="image_only">内容视觉（匿名）</option>
    <option value="occurrence_context">来源上下文</option>
  </select>
  <select id="itemFilter" style="width:190px">
    <option value="pending">仅待审核项</option>
    <option value="approved">已审核项（可修订）</option>
    <option value="all">全部项目</option>
  </select>
  <input id="annotator" placeholder="annotator_id（必填）" style="width:220px">
  <button id="prevBtn" class="secondary" style="width:90px;margin:0">上一项</button>
  <button id="loadBtn" style="width:90px;margin:0">下一项</button>
  <span id="itemPosition" class="hint"></span>
</header>
<main>
  <section class="panel">
    <canvas id="canvas"></canvas>
    <p class="hint">点击图像添加 polygon 点；坐标自动换算到 EXIF 方向修正后的原图坐标。</p>
  </section>
  <section class="panel">
    <div id="itemMeta">尚未加载</div>
    <div id="requirements" class="instructions"></div>
    <div id="contentControls">
      <label>主角色 <span class="required">必选并保存</span></label>
      <select id="role"></select>
      <p id="roleHelp" class="hint"></p>
      <label>代表色资格 <span class="required">必选并保存</span></label>
      <select id="eligible"><option value="true">可提取</option><option value="false">不可提取</option></select>
      <label>资格原因（逗号分隔） <span class="optional">选填，建议不可提取时填写</span></label>
      <input id="reasons">
      <div class="row"><button id="saveRole">保存角色</button><button id="saveEligibility">保存资格</button></div>
      <label>Region 类型 <span class="optional">Pilot 选填；指定标注子集必填</span></label><input id="regionType" value="color_region">
      <div class="row"><button id="saveRegion">保存 polygon region</button><button id="saveMask">保存 polygon + mask</button></div>
      <button class="secondary" id="clearPolygon">清空 polygon</button>
      <label>多色号结构化 JSON <span class="optional">Pilot 选填；多色号子集必填</span></label>
      <textarea id="multiShade" rows="6">{"pairs":[]}</textarea>
      <button id="saveMulti">保存多色号标注</button>
    </div>
    <div id="contextControls" class="hidden">
      <div class="instructions">
        “来源上下文”是这张图片在当前商品目录和 CSV/SKU 记录中的归属信息。
        它只用于判断图片与当前商品/色号的关系，不会送入 A 层内容视觉模型。
        同一图片内容可能出现在多个商品或色号下，因此需要逐 occurrence 审核。
      </div>
      <label>当前 occurrence 的目录、CSV/SKU 与模型融合结果</label>
      <pre id="contextData"></pre>
      <label>人工关系结论 <span class="required">必选并保存</span></label>
      <select id="occurrenceRelation"></select>
      <p id="relationHistory" class="hint"></p>
      <label>审核备注 <span class="optional">选填</span></label>
      <input id="relationNotes">
      <button id="saveRelation">保存来源关系（已有结论时追加修订）</button>
    </div>
    <div id="pilotActions">
      <button id="approvePilot" class="secondary">批准当前 Pilot 项</button>
    </div>
    <div id="fixedActions" class="row hidden"><button id="acceptItem">纳入固定集</button><button class="secondary" id="rejectItem">排除候选</button></div>
    <pre id="status"></pre>
  </section>
</main>
<script>
const state={item:null,image:null,points:[],sets:new Map(),rows:[]};
const $=id=>document.getElementById(id);
const DONE_STATUSES=new Set(['approved','accepted','rejected']);
const ROLE_LABELS={
  single_bullet:'单色膏体图',
  single_swatch:'单色试色图',
  lip_effect:'唇部效果图',
  multi_shade_comparison:'多色号对比图',
  color_card:'色卡／色块图',
  packaging:'包装图',
  text_promo:'文字宣传图',
  invalid:'无效图'
};
const ROLE_HELP={
  single_bullet:'单一色号口红膏体、唇膏棒或膏体近景为主体。',
  single_swatch:'单一色号在手臂、纸面、板面或其他基底上的试色。',
  lip_effect:'真人或模型的唇部上妆效果。',
  multi_shade_comparison:'同一图片包含多个色号的膏体、试色或唇部对比。',
  color_card:'规则排列的色块、色卡、色谱或数字色块。',
  packaging:'外壳、纸盒、瓶体、管体或品牌包装为主体。',
  text_promo:'文字、卖点、成分或色号描述占主导。',
  invalid:'非目标内容、纯装饰、严重损坏、无法识别或无有效信息。'
};
const RELATION_LABELS={
  exact_shade_match:'与当前色号完全匹配',
  contains_context_shade:'多色图中包含当前色号',
  same_product_unspecified_shade:'同商品，但图片未明确色号',
  shade_conflict:'图片色号与当前上下文冲突',
  unrelated:'与当前商品／色号无关',
  insufficient_evidence:'证据不足，无法判断'
};
async function jsonFetch(url,opts={}){const r=await fetch(url,opts);const t=await r.text();if(!r.ok)throw new Error(t);return t?JSON.parse(t):{};}
async function init(){
  const sets=await jsonFetch('/api/sets');
  state.sets=new Map(sets.map(s=>[s.annotation_set_id,s]));
  $('setSelect').innerHTML=sets.map(s=>`<option value="${s.annotation_set_id}">${s.name} ${s.version} [${s.status}]</option>`).join('');
}
function currentItemUrl(){
  const a=$('annotator').value.trim();
  return `/api/items/${state.item.annotation_item_id}${a?`?annotator_id=${encodeURIComponent(a)}`:''}`;
}
function latest(type){return [...(state.item?.events||[])].reverse().find(e=>e.annotation_type===type);}
function filteredRows(rows){
  const mode=$('itemFilter').value;
  if(mode==='approved')return rows.filter(x=>DONE_STATUSES.has(x.status));
  if(mode==='pending')return rows.filter(x=>!DONE_STATUSES.has(x.status));
  return rows;
}
function clearImage(){
  state.image=null;
  const c=$('canvas');
  c.getContext('2d').clearRect(0,0,c.width,c.height);
}
function renderLatestValues(suggestedRelation=null){
  const roleEvent=latest('role');
  if(roleEvent?.after?.role_code&&state.item.role_codes.includes(roleEvent.after.role_code)){
    $('role').value=roleEvent.after.role_code;
    $('roleHelp').textContent=ROLE_HELP[$('role').value]||'';
  }
  const eligibilityEvent=latest('eligibility');
  if(typeof eligibilityEvent?.after?.eligibility_label==='boolean'){
    $('eligible').value=String(eligibilityEvent.after.eligibility_label);
    $('reasons').value=(eligibilityEvent.after.eligibility_reason_codes||[]).join(', ');
  }else{
    $('reasons').value='';
  }
  const relationEvent=latest('occurrence_relation');
  const humanRelation=relationEvent?.after?.relationship_to_context;
  const selected=humanRelation||suggestedRelation;
  if(selected&&state.item.occurrence_relation_codes.includes(selected)){
    $('occurrenceRelation').value=selected;
  }
  $('relationNotes').value=relationEvent?.after?.review_notes||'';
  if(relationEvent){
    $('relationHistory').textContent=`当前人工结论：${RELATION_LABELS[humanRelation]||humanRelation}；审核人：${relationEvent.annotator_id}；保存新值会追加修订并保留旧记录，之后必须重新批准当前 Pilot 项。`;
    $('saveRelation').textContent='保存修订（保留旧审核记录）';
  }else{
    $('relationHistory').textContent=suggestedRelation
      ? `尚无人工结论；当前显示机器建议：${RELATION_LABELS[suggestedRelation]||suggestedRelation}。`
      : '尚无人工结论。';
    $('saveRelation').textContent='保存来源关系';
  }
}
async function loadDirection(direction=1,reset=false){
  const set=$('setSelect').value;
  const visibility=$('visibility').value;
  const setPurpose=state.sets.get(set)?.purpose;
  const sampleOnly=setPurpose==='pilot_review'&&visibility==='occurrence_context';
  const allRows=await jsonFetch(`/api/sets/${set}/items?visibility=${encodeURIComponent(visibility)}&sample_only=${sampleOnly}&limit=500`);
  const rows=filteredRows(allRows);
  state.rows=rows;
  if(!rows.length){
    state.item=null;
    clearImage();
    $('itemMeta').textContent=$('itemFilter').value==='pending'
      ? '没有待审核项；如需修改，请切换到“已审核项（可修订）”'
      : '该视图没有任务项';
    $('itemPosition').textContent=`总计 ${allRows.length}｜已完成 ${allRows.filter(x=>DONE_STATUSES.has(x.status)).length}`;
    return;
  }
  const currentId=state.item?.annotation_item_id;
  const currentIndex=rows.findIndex(x=>x.annotation_item_id===currentId);
  const targetIndex=reset||currentIndex<0
    ? (direction<0?rows.length-1:0)
    : (currentIndex+direction+rows.length)%rows.length;
  const row=rows[targetIndex];
  const a=$('annotator').value.trim();
  state.item=await jsonFetch(`/api/items/${row.annotation_item_id}${a?`?annotator_id=${encodeURIComponent(a)}`:''}`); state.points=[];
  $('itemPosition').textContent=`当前 ${targetIndex+1}/${rows.length}｜总计 ${allRows.length}｜已完成 ${allRows.filter(x=>DONE_STATUSES.has(x.status)).length}`;
  $('role').innerHTML=state.item.role_codes.map(x=>`<option value="${x}">${ROLE_LABELS[x]||x}</option>`).join('');
  $('occurrenceRelation').innerHTML=state.item.occurrence_relation_codes.map(x=>`<option value="${x}">${RELATION_LABELS[x]||x}</option>`).join('');
  $('roleHelp').textContent=ROLE_HELP[$('role').value]||'';
  $('itemMeta').textContent=`匿名项 ${state.item.annotation_item_id} | ${state.item.status} | ${(state.item.coverage_tags||[]).join(', ')}`;
  const isContext=state.item.content_context_visibility==='occurrence_context';
  $('contentControls').classList.toggle('hidden',isContext);
  $('contextControls').classList.toggle('hidden',!isContext);
  const purpose=state.item.annotation_set_purpose;
  $('pilotActions').classList.toggle('hidden',purpose!=='pilot_review');
  $('fixedActions').classList.toggle('hidden',purpose!=='role_eligibility_mask_multishade'||isContext);
  if(purpose==='pilot_review'){
    $('requirements').textContent=isContext
      ? '本项属于 40 条分层抽检。必须：①填写审核人 ID；②核对目录与 CSV/SKU；③确认或修改机器建议并保存人工关系；④点击“批准当前 Pilot 项”。其余 143 条只保留机器预标，不计为人工批准。'
      : '本项必须：①填写审核人 ID；②选择并保存主角色；③选择并保存代表色资格；④点击“批准当前 Pilot 项”。资格原因、region、mask 和多色号 JSON 在 Pilot 中选填。';
  }else{
    $('requirements').textContent='阶段 2.5：审核人 ID、主角色和代表色资格必填；完成后必须选择“纳入固定集”或“排除候选”。被分配到 mask、多色号或盲复核子集时，还须完成对应任务。';
  }
  if(isContext){
    const context=await jsonFetch(state.item.occurrence_context_url);
    const suggested=state.item.sample_model_relationship
      ||context.contexts.map(x=>x.model_relationship).find(Boolean);
    $('contextData').textContent=context.contexts.map((x,index)=>[
      `来源记录 ${index+1}`,
      `品牌目录：${x.brand_folder_raw||'（空）'}`,
      `商品目录：${x.product_folder_raw||'（空）'}`,
      `SKU：${x.sku_id_raw||'（空）'}`,
      `商品名：${x.sku_name_raw||'（空）'}`,
      `组合名称：${x.sku_concat_name_raw||'（空）'}`,
      `上下文色号：${x.sku_color_no_raw||'（空）'}`,
      `CSV 图片字段／序号：${x.source_field||'（空）'} / ${x.image_index}`,
      `模型关系：${RELATION_LABELS[x.model_relationship]||x.model_relationship||'（无）'}`,
      `模型置信度：${x.model_confidence??'（无）'}`,
      `模型冲突：${(x.model_conflicts||[]).join(', ')||'无'}`
    ].join('\n')).join('\n\n');
    renderLatestValues(suggested);
  }else{
    $('contextData').textContent='';
    renderLatestValues();
  }
  clearImage();
  const itemId=state.item.annotation_item_id;
  const img=new Image();
  img.onload=()=>{
    if(state.item?.annotation_item_id!==itemId)return;
    state.image=img;
    draw();
  };
  img.onerror=()=>{
    if(state.item?.annotation_item_id===itemId){
      $('status').textContent='图片加载失败，请点击下一项后再返回重试。';
    }
  };
  img.src=state.item.asset_url+'?item='+encodeURIComponent(itemId)+'&t='+Date.now();
}
function draw(){
  const c=$('canvas'),img=state.image;if(!img)return; const maxW=c.parentElement.clientWidth-28,maxH=window.innerHeight*.72;
  const s=Math.min(maxW/img.width,maxH/img.height,1);c.width=Math.max(1,Math.round(img.width*s));c.height=Math.max(1,Math.round(img.height*s));
  const x=c.getContext('2d');x.drawImage(img,0,0,c.width,c.height);x.strokeStyle='#56e39f';x.fillStyle='#56e39f';x.lineWidth=2;
  const ow=state.item.oriented_width,oh=state.item.oriented_height;
  if(state.points.length){x.beginPath();state.points.forEach((p,i)=>i?x.lineTo(p[0]*c.width/ow,p[1]*c.height/oh):x.moveTo(p[0]*c.width/ow,p[1]*c.height/oh));x.stroke();state.points.forEach(p=>{x.beginPath();x.arc(p[0]*c.width/ow,p[1]*c.height/oh,4,0,Math.PI*2);x.fill();});}
}
$('canvas').onclick=e=>{if(!state.image)return;const r=e.target.getBoundingClientRect();state.points.push([(e.clientX-r.left)*state.item.oriented_width/r.width,(e.clientY-r.top)*state.item.oriented_height/r.height]);draw();};
async function post(type,payload){if(!state.item)throw Error('未加载');const a=$('annotator').value.trim();if(!a)throw Error('annotator_id 必填');const old=latest(type);const body={annotator_id:a,annotation_type:type,payload,supersedes_event_id:old?.annotation_event_id||null};const out=await jsonFetch(`/api/items/${state.item.annotation_item_id}/events`,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});state.item=await jsonFetch(currentItemUrl());$('itemMeta').textContent=`匿名项 ${state.item.annotation_item_id} | ${state.item.status} | ${(state.item.coverage_tags||[]).join(', ')}`;$('status').textContent=old?`修订已保存；旧记录仍保留。必须重新点击“批准当前 Pilot 项”。\n${JSON.stringify(out,null,2)}`:JSON.stringify(out,null,2);renderLatestValues(state.item.sample_model_relationship);}
$('saveRole').onclick=()=>post('role',{role_code:$('role').value}).catch(e=>$('status').textContent=e);
$('saveEligibility').onclick=()=>post('eligibility',{eligibility_label:$('eligible').value==='true',eligibility_reason_codes:$('reasons').value.split(',').map(x=>x.trim()).filter(Boolean)}).catch(e=>$('status').textContent=e);
$('saveRegion').onclick=()=>post('region',{region_type:$('regionType').value,polygon_image:state.points}).catch(e=>$('status').textContent=e);
$('saveMask').onclick=()=>post('mask',{region_type:$('regionType').value,polygon_image:state.points}).catch(e=>$('status').textContent=e);
$('saveMulti').onclick=()=>{try{post('multi_shade',{multi_shade:JSON.parse($('multiShade').value)}).catch(e=>$('status').textContent=e)}catch(e){$('status').textContent=e}};
$('saveRelation').onclick=()=>post('occurrence_relation',{relationship_to_context:$('occurrenceRelation').value,review_notes:$('relationNotes').value.trim()}).catch(e=>$('status').textContent=e);
$('clearPolygon').onclick=()=>{state.points=[];draw();};
$('approvePilot').onclick=async()=>{try{const a=$('annotator').value.trim();if(!a)throw Error('annotator_id 必填');await jsonFetch(`/api/items/${state.item.annotation_item_id}/approve-pilot`,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({annotator_id:a})});$('status').textContent='Pilot 项已批准';await loadDirection(1);}catch(e){$('status').textContent=e}};
async function decide(decision){const out=await jsonFetch(`/api/items/${state.item.annotation_item_id}/decision`,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({decision})});$('status').textContent=JSON.stringify(out,null,2);await loadDirection(1);}
$('acceptItem').onclick=()=>decide('accepted').catch(e=>$('status').textContent=e);
$('rejectItem').onclick=()=>decide('rejected').catch(e=>$('status').textContent=e);
$('prevBtn').onclick=()=>loadDirection(-1).catch(e=>$('status').textContent=e);
$('loadBtn').onclick=()=>loadDirection(1).catch(e=>$('status').textContent=e);
$('setSelect').onchange=()=>loadDirection(1,true).catch(e=>$('status').textContent=e);
$('visibility').onchange=()=>loadDirection(1,true).catch(e=>$('status').textContent=e);
$('itemFilter').onchange=()=>loadDirection(1,true).catch(e=>$('status').textContent=e);
$('role').onchange=()=>$('roleHelp').textContent=ROLE_HELP[$('role').value]||'';
$('annotator').onchange=()=>state.item&&jsonFetch(currentItemUrl()).then(item=>{state.item=item;renderLatestValues(state.item.sample_model_relationship);}).catch(e=>$('status').textContent=e);
init().then(()=>loadDirection(1,true)).catch(e=>$('status').textContent=e);
</script>
</body></html>"""
