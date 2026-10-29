export const formatCredits = (value: number) =>
  value.toLocaleString("zh-CN", { maximumFractionDigits: 4 });

export const validCredits = (value: number | null) =>
  value !== null && Number.isFinite(value) && value >= 0 && value <= 2147483647 &&
  /^\d+(?:\.\d{1,4})?$/.test(String(value));
