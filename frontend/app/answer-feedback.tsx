"use client";

import { FormEvent, useState } from "react";
import { Loader2, ThumbsDown, ThumbsUp } from "lucide-react";
import { request } from "./api";
import { Modal } from "./modal";

export type Feedback = {
  id: string;
  rating: "up" | "down";
  categories: string[];
  comment: string | null;
};

const CATEGORIES = [
  ["missing_source", "资料缺失"],
  ["retrieval_error", "检索错误"],
  ["answer_error", "回答错误"],
  ["citation_error", "引用错误"],
] as const;

export function AnswerFeedback({
  messageId,
  value,
  onChange,
}: {
  messageId: string;
  value?: Feedback | null;
  onChange: (value: Feedback | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const [categories, setCategories] = useState<string[]>([]);
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function save(rating: "up" | "down") {
    setBusy(true);
    setError("");
    try {
      const result = await request<Feedback>("/feedback", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message_id: messageId,
          rating,
          ...(rating === "down"
            ? { categories, comment: comment.trim() || null }
            : {}),
        }),
      });
      onChange(result);
      setOpen(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : "反馈未能保存，请重试");
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    setBusy(true);
    setError("");
    try {
      await request(`/feedback/${messageId}`, { method: "DELETE" });
      onChange(null);
      setOpen(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : "反馈未能撤销，请重试");
    } finally {
      setBusy(false);
    }
  }

  function down() {
    setCategories(value?.rating === "down" ? value.categories : []);
    setComment(value?.rating === "down" ? value.comment || "" : "");
    setError("");
    setOpen(true);
  }
  function submit(event: FormEvent) {
    event.preventDefault();
    if (categories.length) void save("down");
  }

  return (
    <>
      <span className="feedback-actions" aria-label="回答反馈">
        <button
          className={`icon-button ${value?.rating === "up" ? "feedback-selected" : ""}`}
          title={value?.rating === "up" ? "撤销点赞" : "点赞"}
          aria-label={value?.rating === "up" ? "撤销点赞" : "点赞"}
          aria-pressed={value?.rating === "up"}
          disabled={busy}
          onClick={() => void (value?.rating === "up" ? remove() : save("up"))}
        >
          <ThumbsUp size={15} />
        </button>
        <button
          className={`icon-button ${value?.rating === "down" ? "feedback-selected" : ""}`}
          title={value?.rating === "down" ? "修改负面反馈" : "点踩"}
          aria-label={value?.rating === "down" ? "修改负面反馈" : "点踩"}
          aria-pressed={value?.rating === "down"}
          disabled={busy}
          onClick={down}
        >
          <ThumbsDown size={15} />
        </button>
      </span>
      {!open && error && (
        <span className="feedback-error" role="alert">
          {error}
        </span>
      )}
      {open && (
        <Modal title="反馈问题" onClose={() => setOpen(false)} busy={busy}>
          <form className="modal-form feedback-form" onSubmit={submit}>
            <fieldset className="feedback-categories">
              <legend>问题类型</legend>
              {CATEGORIES.map(([id, label]) => (
                <label key={id}>
                  <input
                    type="checkbox"
                    checked={categories.includes(id)}
                    disabled={busy}
                    onChange={(e) =>
                      setCategories(
                        e.target.checked
                          ? [...categories, id]
                          : categories.filter((item) => item !== id),
                      )
                    }
                  />
                  {label}
                </label>
              ))}
            </fieldset>
            <label>
              补充说明<span className="optional">选填</span>
              <textarea
                value={comment}
                onChange={(e) => setComment(e.target.value)}
                rows={3}
                maxLength={2000}
                disabled={busy}
              />
            </label>
            {error && (
              <p className="form-error" role="alert">
                {error}
              </p>
            )}
            <div className="modal-footer">
              {value?.rating === "down" && (
                <button
                  className="text-button feedback-remove"
                  type="button"
                  disabled={busy}
                  onClick={() => void remove()}
                >
                  撤销反馈
                </button>
              )}
              <button
                className="secondary"
                type="button"
                disabled={busy}
                onClick={() => setOpen(false)}
              >
                取消
              </button>
              <button
                className="primary"
                disabled={busy || !categories.length}
                type="submit"
              >
                {busy && <Loader2 size={15} className="spin" />}
                保存反馈
              </button>
            </div>
          </form>
        </Modal>
      )}
    </>
  );
}
