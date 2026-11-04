export const MiB = 1024 ** 2;
export const GiB = 1024 ** 3;
export function bytes(value: number) {
  if (value >= GiB) return `${(value / GiB).toLocaleString(undefined, { maximumFractionDigits: 2 })} GiB`;
  if (value >= MiB) return `${(value / MiB).toLocaleString(undefined, { maximumFractionDigits: 2 })} MiB`;
  return `${value.toLocaleString()} B`;
}
export type StoragePackage = { id: string; name: string; storage_bytes: number; download_bytes: number;
  days: number; price_credits: number | null; enabled: boolean; order: number };
export type Entitlement = { id: string; name: string; kind: string; storage_bytes: number; download_bytes: number;
  remaining_bytes: number; price_credits: string; created_at: string; expires_at: string };
export type StorageAllowances = { used_bytes: number; limit_bytes: number; download_remaining_bytes: number;
  retention_days: number; download_resets_at: string; over_quota_since: string | null;
  cleanup_due_at: string | null; entitlements: Entitlement[]; history: Entitlement[];
  packages: StoragePackage[]; config_version: number;
  breakdown: Record<string, number>; download_usage?: { model_read: number; user_download: number } };
export type StorageQuota = { version: number; free_storage_bytes: number; free_download_bytes: number;
  output_limit_bytes: number; retention_days: number; packages: StoragePackage[]; costs: Record<string, string> };
