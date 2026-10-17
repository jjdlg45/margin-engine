import { useEffect, useState } from "react";

/** True below 640px - charts switch to their stacked/horizontal variants. */
export function useCompact(): boolean {
  const [compact, setCompact] = useState(
    () => typeof window !== "undefined" && window.innerWidth < 640,
  );
  useEffect(() => {
    const mq = window.matchMedia("(max-width: 639px)");
    const onChange = () => setCompact(mq.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);
  return compact;
}
