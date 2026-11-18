import { type CSSProperties, type ReactNode, useEffect, useRef, useState } from "react";
import { Badge, Button, Heading, Inline, Prose, Stack, Text, tokens } from "@nimblebrain/synapse/ui";
import type { Interaction } from "../types";
import { fmtDateTime, markdownToHtml } from "../format";

const inputStyle: CSSProperties = {
  width: "100%",
  boxSizing: "border-box",
  fontFamily: tokens.fontSans,
  fontSize: tokens.textSmSize,
  lineHeight: tokens.textSmLine,
  color: tokens.fg,
  background: tokens.bg,
  border: `1px solid ${tokens.border}`,
  borderRadius: tokens.radiusSm,
  padding: "0.45rem 0.6rem",
};

/** A labeled text input or textarea for forms. */
export function Field({
  label,
  value,
  onChange,
  placeholder,
  type = "text",
  required = false,
  textarea = false,
  rows = 4,
  autoFocus = false,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  type?: string;
  required?: boolean;
  textarea?: boolean;
  rows?: number;
  autoFocus?: boolean;
}) {
  return (
    <label style={{ display: "flex", flexDirection: "column", gap: "0.3rem" }}>
      <Text size="xs" tone="muted" weight="medium">
        {label}
        {required ? <span style={{ color: tokens.danger }}> *</span> : null}
      </Text>
      {textarea ? (
        <textarea
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          rows={rows}
          // biome-ignore lint: autoFocus is intentional inside modal forms.
          autoFocus={autoFocus}
          style={{ ...inputStyle, resize: "vertical" }}
        />
      ) : (
        <input
          type={type}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          autoFocus={autoFocus}
          style={inputStyle}
        />
      )}
    </label>
  );
}

/** A label/value row for detail panels. Hidden when there's no value. */
export function FieldRow({ label, children }: { label: string; children?: ReactNode }) {
  if (children === null || children === undefined || children === "") return null;
  return (
    <Inline gap="0.75rem" align="baseline">
      <Text size="xs" tone="faint" style={{ width: 96, flexShrink: 0 }}>
        {label}
      </Text>
      <div style={{ minWidth: 0 }}>{children}</div>
    </Inline>
  );
}

/** A centered modal overlay used by the create/edit/log/merge forms. */
export function Modal({
  title,
  onClose,
  children,
  footer,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
}) {
  return (
    <div
      onClick={onClose}
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 50,
        background: "rgba(15, 23, 42, 0.45)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: "clamp(0.75rem, 4vw, 1.5rem)",
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        style={{
          width: "min(440px, 100%)",
          maxHeight: "90vh",
          overflowY: "auto",
          background: tokens.bg,
          border: `1px solid ${tokens.border}`,
          borderRadius: tokens.radiusMd,
          boxShadow: "0 20px 60px rgba(15, 23, 42, 0.25)",
          padding: "1.25rem",
        }}
      >
        <Stack gap="1rem">
          <Heading size="sm">{title}</Heading>
          {children}
          <Inline gap="0.5rem" justify="end">
            {footer ?? (
              <Button variant="ghost" onClick={onClose}>
                Close
              </Button>
            )}
          </Inline>
        </Stack>
      </div>
    </div>
  );
}

/** The reverse-chronological interaction list for a contact or organization. */
export function Timeline({ interactions }: { interactions: Interaction[] }) {
  if (interactions.length === 0) {
    return (
      <Text size="sm" tone="faint">
        No interactions logged yet.
      </Text>
    );
  }
  return (
    <Stack gap="0.85rem">
      {interactions.map((ix) => (
        <Stack key={ix.id} gap="0.2rem">
          <Inline gap="0.5rem" align="baseline">
            <Badge tone="neutral">{ix.kind}</Badge>
            {ix.direction ? (
              <Text size="sm" tone="muted">
                {ix.direction}
              </Text>
            ) : null}
            <Text size="sm" weight="medium">
              {ix.summary}
            </Text>
          </Inline>
          {ix.body ? (
            <Text size="sm" tone="muted" style={{ whiteSpace: "pre-wrap" }}>
              {ix.body}
            </Text>
          ) : null}
          <Inline gap="0.6rem">
            <Text size="xs" tone="faint">
              {fmtDateTime(ix.occurred_at)}
            </Text>
            {ix.follow_up_at ? (
              <Text size="xs" tone="accent">
                follow up {fmtDateTime(ix.follow_up_at)}
              </Text>
            ) : null}
          </Inline>
        </Stack>
      ))}
    </Stack>
  );
}

/** Render a research dossier (markdown), or a muted placeholder. */
export function Dossier({ markdown }: { markdown: string | null }) {
  if (!markdown) {
    return (
      <Text size="sm" tone="faint">
        No dossier yet.
      </Text>
    );
  }
  return <Prose html={markdownToHtml(markdown)} />;
}

/** One entry of an {@link OverflowMenu}. */
export interface MenuItem {
  label: string;
  onSelect: () => void;
}

/**
 * The actions a page takes on its record, behind one ⋮ button: a page header
 * then carries a single control, and the actions read as a list when wanted.
 */
export function OverflowMenu({ label, items }: { label: string; items: MenuItem[] }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const escape = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
        ref.current?.querySelector("button")?.focus();
      }
    };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);
  return (
    <div ref={ref} style={{ position: "relative" }}>
      <Button
        variant="ghost"
        size="sm"
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        style={{ fontSize: "1.1rem", lineHeight: 1, padding: "0.3rem 0.5rem" }}
      >
        ⋮
      </Button>
      {open ? (
        <div
          role="menu"
          aria-label={label}
          style={{
            position: "absolute",
            right: 0,
            top: "calc(100% + 4px)",
            zIndex: 20,
            minWidth: 180,
            background: tokens.bg,
            border: `1px solid ${tokens.border}`,
            borderRadius: tokens.radiusSm,
            boxShadow: tokens.shadowLg,
            padding: "0.3rem",
          }}
        >
          {items.map((item) => (
            <button
              key={item.label}
              type="button"
              role="menuitem"
              onClick={() => {
                setOpen(false);
                item.onSelect();
              }}
              style={{
                display: "block",
                width: "100%",
                textAlign: "left",
                background: "none",
                border: "none",
                borderRadius: tokens.radiusXs,
                padding: "0.4rem 0.6rem",
                whiteSpace: "nowrap",
                font: "inherit",
                fontSize: tokens.textSmSize,
                color: tokens.fg,
                cursor: "pointer",
              }}
              onMouseEnter={(e) => (e.currentTarget.style.background = tokens.bgSubtle)}
              onMouseLeave={(e) => (e.currentTarget.style.background = "none")}
            >
              {item.label}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}
