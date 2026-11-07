"use client";

import { useId, useState } from "react";
import { Plus, Trash2 } from "lucide-react";
import { Dialog } from "../components/ui/dialog";
import * as Tabs from "../components/ui/tabs";
import { useAdminResource } from "./admin/admin-ui";
import { validCredits } from "./credit-format";
import type { ImageConfig } from "./model-types";
import "./image-config-fields.css";

type Operation = "generate" | "edit";
type Spec = ImageConfig["specs"][number];
type Preset = {
  id: string; name: string; revision: number; verified_at: string; source: string;
  protocols: ImageConfig["protocol"][]; operations: Operation[]; max_count: number; max_references: number;
  size_label: string; quality_label: string;
  sizes: { value: string; label: string; ratio: string }[]; qualities: { value: string; label: string }[];
};
const opLabel = { generate: "图片生成", edit: "图片编辑" };
const protocols = { openai_images: "OpenAI Images", responses: "Responses 原生生图", gemini: "Gemini 原生" };
const key = (s: Pick<Spec, "size" | "quality">) => `${s.size}|${s.quality}`;
const priced = (s: Spec) => s.credits_per_image !== null || s.cost_per_image !== null;

// Only explicit edits repair defaults. Rendering old configuration never changes its payload.
function withSpecs(value: ImageConfig, specs: Spec[]): ImageConfig {
  const defaults = { ...value.defaults };
  for (const op of ["generate", "edit"] as const) {
    const current = defaults[op] ?? { size: value.default_size, quality: value.default_quality };
    const rows = specs.filter(s => s.operation === op);
    const next = rows.find(s => key(s) === key(current)) ?? rows[0];
    if (next) defaults[op] = { size: next.size, quality: next.quality };
    else delete defaults[op];
  }
  return { ...value, specs, defaults };
}

export function ImageConfigFields({ value, change }: { value: ImageConfig; change: (value: ImageConfig) => void }) {
  const instanceId = useId();
  const catalog = useAdminResource<Preset[]>("/site/admin/model-connections/image-presets");
  const [operation, setOperation] = useState<Operation>("generate");
  const [batch, setBatch] = useState("");
  const [notice, setNotice] = useState("");
  const [customOpen, setCustomOpen] = useState(false);
  const [pending, setPending] = useState<{ next: ImageConfig; title: string; details: string[] } | null>(null);
  const p = catalog.data?.find(p => p.id === value.preset_id);
  const inferred = !value.preset_id && value.specs.length > 0 ? catalog.data?.find(p =>
    p.protocols.includes(value.protocol) && value.max_count <= p.max_count && value.max_references <= p.max_references &&
    value.specs.every(s => p.sizes.some(x => x.value === s.size) && p.qualities.some(x => x.value === s.quality))) : undefined;
  const rows = value.specs.filter(s => s.operation === operation);
  const currentDefault = value.defaults?.[operation] ?? { size: value.default_size, quality: value.default_quality };
  const qualityLabel = p?.quality_label ?? (value.protocol === "gemini" ? "分辨率" : "质量");
  const sizeLabel = p?.size_label ?? (value.protocol === "gemini" ? "宽高比" : "尺寸");
  const label = (s: Pick<Spec, "size" | "quality">) => `${s.size} · ${p?.qualities.find(q => q.value === s.quality)?.label ?? s.quality}`;
  function confirm(next: ImageConfig, title: string, details: string[]) { setPending({ next, title, details }); }
  function choosePreset(id: string) {
    if (id === (value.preset_id ?? "")) return;
    if (!id) { confirm({ ...value, preset_id: null, preset_revision: null }, "使用自定义兼容配置", ["保留全部规格、默认值和价格。"]); return; }
    const target = catalog.data?.find(p => p.id === id);
    if (!target) return;
    const supported = (s: Spec) => target.sizes.some(x => x.value === s.size) && target.qualities.some(x => x.value === s.quality);
    const retained = value.specs.filter(supported), removed = value.specs.filter(s => !supported(s));
    const protocol = target.protocols.includes(value.protocol) ? value.protocol : target.protocols[0];
    const next = withSpecs({ ...value, preset_id: id, preset_revision: target.revision, protocol,
      max_count: Math.min(value.max_count, protocol === "openai_images" ? target.max_count : 1),
      max_references: Math.min(value.max_references, target.max_references) }, retained);
    confirm(next, `应用 ${target.name}`, [
      `保留 ${retained.length} 项规格及其价格；移除 ${removed.length} 项不兼容规格。`,
      ...removed.map(s => `${opLabel[s.operation]}：${s.size} / ${s.quality}（${s.credits_per_image ?? "未定价"} 积分）`),
      `协议：${protocols[value.protocol]} → ${protocols[protocol]}`,
      `最多生成 ${next.max_count} 张，最多参考 ${next.max_references} 张。`,
      ...(["generate", "edit"] as const).map(op => `${opLabel[op]}默认：${label(value.defaults?.[op] ?? { size: value.default_size, quality: value.default_quality })} → ${next.defaults?.[op] ? label(next.defaults[op]!) : "待选择"}`),
    ]);
  }
  function toggle(size: string, quality: string) {
    setNotice("");
    const found = rows.find(s => s.size === size && s.quality === quality);
    if (!found && value.specs.length >= 100) { setNotice("最多开放 100 项规格，请先移除不需要的规格。"); return; }
    const next = withSpecs(value, found ? value.specs.filter(s => s !== found) : [...value.specs,
      { operation, size, quality, credits_per_image: null, cost_per_image: null }]);
    if (found && priced(found)) confirm(next, "移除已定价规格", [`${opLabel[operation]}：${label(found)}`, "将移除此规格及价格；现有图片任务不受影响。"]);
    else change(next);
  }
  function update(index: number, patch: Partial<Spec>) {
    change(withSpecs(value, value.specs.map((s, i) => i === index ? { ...s, ...patch } : s)));
  }
  function copyPrices() {
    const sources = value.specs.filter(s => s.operation === "generate" && s.credits_per_image !== null);
    const additions = sources.filter(s => !value.specs.some(t => t.operation === "edit" && key(s) === key(t))).map(s => ({ ...s, operation: "edit" as const }));
    if (value.specs.length + additions.length > 100) { setNotice("复制后超过 100 项，请先减少规格。"); return; }
    confirm(withSpecs(value, [...value.specs.map(s => s.operation === "edit" ? { ...s, ...sources.find(t => key(t) === key(s)), operation: "edit" as const } : s), ...additions]),
      "复制生成价格到编辑", ["相同规格的编辑价格将被替换，缺少的编辑规格将添加；请核对后保存。"]);
  }
  return <section className="model-form-section image-config" data-image-config={instanceId}><h2>图片能力与价格</h2>
    <div className="provider-form-grid">{(["generate", "edit"] as const).map(op => <label className="provider-enabled" key={op}>
      <input type="checkbox" checked={value[op]} onChange={e => { change({ ...value, [op]: e.target.checked }); if (e.target.checked) setOperation(op); }} />{opLabel[op]}</label>)}</div>
    {(value.generate || value.edit) && <>
      <div className="provider-form-grid">
        <label>图片能力预设<select aria-label="图片能力预设" value={value.preset_id ?? ""} onChange={e => choosePreset(e.target.value)}>
          <option value="">自定义兼容配置</option>
          {catalog.data?.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select></label>
        <label>图片协议<select value={value.protocol} onChange={e => {
          const protocol = e.target.value as ImageConfig["protocol"];
          confirm({ ...value, protocol, max_count: protocol === "openai_images" ? value.max_count : 1 }, "更换图片协议", [`${protocols[value.protocol]} → ${protocols[protocol]}`, "已有规格和价格保留，请核对新接口支持情况。"]);
        }}>{Object.entries(protocols).filter(([id]) => !p || p.protocols.includes(id as ImageConfig["protocol"])).map(([id, name]) => <option value={id} key={id}>{name}</option>)}</select></label>
        {value.protocol === "responses" && <label>图片工具模型<input required maxLength={200} value={value.image_model ?? ""} onChange={e => change({ ...value, image_model: e.target.value })} /></label>}
        <label>最多生成张数<input type="number" min={1} max={value.protocol === "openai_images" ? p?.max_count ?? 4 : 1} value={value.max_count} onChange={e => change({ ...value, max_count: Number(e.target.value) })} /></label>
        <label>最多参考图<input type="number" min={1} max={p?.max_references ?? 8} value={value.max_references} onChange={e => change({ ...value, max_references: Number(e.target.value) })} /></label>
      </div>
      {catalog.error && <p role="alert">{catalog.error} <button type="button" className="secondary" onClick={() => void catalog.reload()}>重新加载选项</button></p>}
      {p && <p className="image-preset-source"><a href={p.source} target="_blank" rel="noreferrer">官方参数依据</a><span>核对于 {p.verified_at}</span><span>当前供应商支持情况需单独测试</span></p>}
      {inferred && <p className="image-preset-source">现有规格匹配 {inferred.name}，可在上方选择应用；当前配置尚未更改。</p>}
      <Tabs.Root value={operation} onValueChange={v => setOperation(v as Operation)}>
        <Tabs.List aria-label="图片操作"><Tabs.Trigger value="generate">图片生成</Tabs.Trigger><Tabs.Trigger value="edit">图片编辑</Tabs.Trigger></Tabs.List>
      </Tabs.Root>
      {!value[operation] && <p className="image-preset-source">此操作未启用；可先配置规格和价格。</p>}
      {p ? <fieldset className="image-spec-options"><legend>开放{sizeLabel}与{qualityLabel}</legend>
        {p.sizes.map(size => <div className="image-spec-option" key={size.value}>
          <div className="image-size-label"><span className="image-ratio-frame"><span style={{ aspectRatio: size.ratio.replace(":", "/"), width: Number(size.ratio.split(":")[0]) >= Number(size.ratio.split(":")[1]) ? "28px" : undefined, height: Number(size.ratio.split(":")[0]) < Number(size.ratio.split(":")[1]) ? "28px" : undefined }} /></span><span>{size.label}<small>{size.value}</small></span></div>
          <div className="image-quality-options">{p.qualities.map(q => <label key={q.value}><input type="checkbox" checked={rows.some(s => s.size === size.value && s.quality === q.value)} onChange={() => toggle(size.value, q.value)} />{q.label}</label>)}</div>
        </div>)}
      </fieldset> : <details className="image-custom-options" open={customOpen} onToggle={e => setCustomOpen(e.currentTarget.open)}><summary>自定义兼容配置</summary>
        <div className="image-price-actions"><button className="secondary" type="button" disabled={value.specs.length >= 100} onClick={() => change(withSpecs(value, [...value.specs, { operation, size: value.default_size, quality: value.default_quality, credits_per_image: null, cost_per_image: null }]))}><Plus size={16} />添加规格</button></div>
        {rows.map(s => <div className="provider-form-grid" key={value.specs.indexOf(s)}><label>{sizeLabel}<input required maxLength={40} value={s.size} onChange={e => update(value.specs.indexOf(s), { size: e.target.value })} /></label><label>{qualityLabel}<input required maxLength={40} value={s.quality} onChange={e => update(value.specs.indexOf(s), { quality: e.target.value })} /></label></div>)}
      </details>}
      <div className="image-price-actions">
        <label>批量每张积分<input aria-label="批量每张积分" type="number" min={0} step="0.0001" value={batch} onChange={e => setBatch(e.target.value)} /></label>
        <button type="button" className="secondary" disabled={!rows.length || batch === "" || !validCredits(Number(batch))} onClick={() => confirm({ ...value, specs: value.specs.map(s => s.operation === operation ? { ...s, credits_per_image: Number(batch) } : s) }, "批量设置价格", [`将 ${opLabel[operation]}的 ${rows.length} 项规格设为每张 ${batch} 积分。`])}>应用到当前操作</button>
        <button type="button" className="secondary" disabled={!value.specs.some(s => s.operation === "generate" && s.credits_per_image !== null)} onClick={copyPrices}>复制生成价格到编辑</button>
      </div>
      <div className="image-price-list">{rows.map(s => {
        const index = value.specs.indexOf(s);
        return <div className="image-price-row" key={index}>
          <strong>{label(s)}</strong>
          <label>每张积分<input aria-label={`${s.size} ${s.quality} 每张积分`} required type="number" min={0} step="0.0001" value={s.credits_per_image ?? ""} onChange={e => update(index, { credits_per_image: e.target.value === "" ? null : Number(e.target.value) })} /></label>
          <label>成本参考（积分/张）<input type="number" min={0} step="0.0001" value={s.cost_per_image ?? ""} onChange={e => update(index, { cost_per_image: e.target.value === "" ? null : Number(e.target.value) })} /></label>
          <button type="button" className="icon-button" title="移除规格" aria-label={`移除 ${s.size} ${s.quality}`} onClick={() => {
            const next = withSpecs(value, value.specs.filter((_, i) => i !== index));
            if (priced(s)) confirm(next, "移除已定价规格", [label(s), "将移除此规格及价格；现有图片任务不受影响。"]); else change(next);
          }}><Trash2 size={16} /></button>
        </div>;
      })}</div>
      <label className="image-default-label">{opLabel[operation]}默认规格<select aria-label={`${opLabel[operation]}默认规格`} value={rows.some(s => key(s) === key(currentDefault)) ? key(currentDefault) : ""} onChange={e => {
        const s = rows.find(s => key(s) === e.target.value);
        if (s) change({ ...value, defaults: { ...value.defaults, [operation]: { size: s.size, quality: s.quality } } });
      }}><option value="">请选择已定价规格</option>{rows.map((s, i) => <option key={i} value={key(s)} disabled={s.credits_per_image === null}>{label(s)}{s.credits_per_image === null ? " · 未定价" : ""}</option>)}</select></label>
      {notice && <p role="alert">{notice}</p>}
    </>}
    {pending && <Dialog title={pending.title} restoreFocusSelector={`[data-image-config="${instanceId}"] select[aria-label="图片能力预设"]`} onClose={() => setPending(null)}><div className="image-config-confirm">{pending.details.map((line, i) => <p key={i}>{line}</p>)}</div><div className="image-price-actions"><button type="button" className="secondary" onClick={() => setPending(null)}>取消</button><button type="button" className="primary" onClick={() => { change(pending.next); setPending(null); }}>确认修改草稿</button></div></Dialog>}
  </section>;
}
