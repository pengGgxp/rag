import type { SkillPackagePreview } from "./skill-package-upload";
import "./skill-package-review.css";

export function SkillPackagePicker({
  packages,
  selected,
  activePackage,
  onSelectedChange,
  onActivePackageChange,
}: {
  packages: SkillPackagePreview[];
  selected: number[];
  activePackage: number;
  onSelectedChange: (ids: number[]) => void;
  onActivePackageChange: (id: number) => void;
}) {
  const allSelected = packages.length > 0 && packages.every(item => selected.includes(item.id));

  return <div className="skill-package-list-panel">
    <div className="skill-package-selection">
      <span role="status">已选择 <strong>{selected.length}</strong> / {packages.length}</span>
      <div>
        <button type="button" disabled={allSelected} onClick={() => onSelectedChange(packages.map(item => item.id))}>全选</button>
        <button type="button" disabled={!selected.length} onClick={() => onSelectedChange([])}>清空</button>
      </div>
    </div>
    <nav className="skill-package-list" aria-label="Skill 列表">{packages.map(item => {
      const isSelected = selected.includes(item.id);
      const isPreviewing = item.id === activePackage;
      return <div className="skill-package-choice" data-selected={isSelected} data-previewing={isPreviewing} key={item.id}>
        <label><input type="checkbox" checked={isSelected} aria-label={`导入${item.name}`}
          onChange={event => onSelectedChange(event.target.checked
            ? [...selected, item.id] : selected.filter(value => value !== item.id))} />
          <span><strong>{item.name}</strong><small>{item.description || item.folder}</small></span>
        </label>
        <button type="button" aria-current={isPreviewing ? "true" : undefined} aria-label={`查看${item.name}正文`}
          onClick={() => onActivePackageChange(item.id)}>{isPreviewing ? "预览中" : "预览"}</button>
      </div>;
    })}</nav>
  </div>;
}
