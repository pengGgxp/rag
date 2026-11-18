export const MAX_SKILL_SIZE_MIB = 200;
export const MAX_SKILL_BYTES = MAX_SKILL_SIZE_MIB * 1024 * 1024;

export function skillUploadError(files: readonly { size: number }[], isFolder = false): string | null {
  if (files.some(file => file.size > MAX_SKILL_BYTES))
    return isFolder ? `单个 Skill 文件不能超过 ${MAX_SKILL_SIZE_MIB} MiB` : `Skill 包不能超过 ${MAX_SKILL_SIZE_MIB} MiB`;
  if (isFolder && files.reduce((total, file) => total + file.size, 0) > MAX_SKILL_BYTES)
    return `Skill 总大小不能超过 ${MAX_SKILL_SIZE_MIB} MiB`;
  return null;
}
