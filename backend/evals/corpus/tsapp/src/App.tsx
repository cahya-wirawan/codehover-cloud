import { useState } from "react";

import { clamp, type Range } from "./math";

const LIMITS: Range = { min: 0, max: 10 };

export function CounterButton({ label }: { label: string }) {
  const [value, setValue] = useState(0);
  const next = () => setValue((v) => clamp(v + 1, LIMITS));
  return <button onClick={next}>{label}: {value}</button>;
}

export function App() {
  return <main><CounterButton label="Clicks" /></main>;
}

function formatLabel(label: string): string {
  return label.trim().toUpperCase();
}

export default formatLabel;
