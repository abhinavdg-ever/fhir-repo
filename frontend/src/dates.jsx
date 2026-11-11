import React, { useEffect, useState } from "react";

// The API and FHIR use ISO dates (YYYY-MM-DD); people see and type MM/DD/YYYY.

export function toUS(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || "");
  return m ? `${m[2]}/${m[3]}/${m[1]}` : iso || "";
}

export function fromUS(text) {
  const m = /^(\d{2})\/(\d{2})\/(\d{4})$/.exec(text || "");
  if (!m) return "";
  const [, mm, dd, yyyy] = m;
  const d = new Date(Date.UTC(+yyyy, +mm - 1, +dd));
  const valid = d.getUTCFullYear() === +yyyy && d.getUTCMonth() === +mm - 1 && d.getUTCDate() === +dd;
  return valid ? `${yyyy}-${mm}-${dd}` : "";
}

// Insert the slashes while typing: "0915" -> "09/15", "09151990" -> "09/15/1990".
function mask(raw) {
  const digits = raw.replace(/\D/g, "").slice(0, 8);
  return [digits.slice(0, 2), digits.slice(2, 4), digits.slice(4)].filter(Boolean).join("/");
}

/** Text input in MM/DD/YYYY. `value` and `onChange` use ISO; onChange("") while incomplete or invalid. */
export function DateInput({ value, onChange, className = "", ...rest }) {
  const [text, setText] = useState(toUS(value));

  useEffect(() => {
    // Follow outside changes (e.g. a new plan) without fighting the user's typing.
    if ((value || "") !== fromUS(text)) setText(toUS(value));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);

  const invalid = text.length === 10 && !fromUS(text);
  return (
    <input
      type="text"
      inputMode="numeric"
      placeholder="mm/dd/yyyy"
      maxLength={10}
      value={text}
      className={`date-input ${invalid ? "invalid" : ""} ${className}`}
      onChange={(e) => {
        const next = mask(e.target.value);
        setText(next);
        onChange(fromUS(next));
      }}
      {...rest}
    />
  );
}
