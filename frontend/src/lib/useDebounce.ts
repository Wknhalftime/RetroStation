import { useEffect, useState } from "react";

// The value once it has stopped changing for *delay* ms: a search box asks the server once per
// pause, not once per keystroke.
export function useDebounce<T>(value: T, delay: number): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), delay);
    return () => clearTimeout(timer);
  }, [value, delay]);
  return settled;
}
