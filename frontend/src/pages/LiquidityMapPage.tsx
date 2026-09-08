import { useEffect, useRef } from "react";
import { useLiveMakerSymbols } from "../hooks/useLiveMakerSymbols";
import { useSearchParams } from "react-router-dom";
import { config } from "../lib/config";

export function LiquidityMapPage() {
  const live = useLiveMakerSymbols();
  const frame = useRef<HTMLIFrameElement>(null);
  const publishLive = () => frame.current?.contentWindow?.postMessage({type:"sandbox-maker-live", symbols:[...live]}, origin);
  useEffect(() => { publishLive(); }, [live]);
  const [params] = useSearchParams();
  const symbol = params.get("symbol") || "BTCUSDT";
  const origin = new URL(config.apiBaseUrl, window.location.origin).origin;
  return <iframe ref={frame} onLoad={publishLive}
    title="沙盒流动性地图"
    src={`${origin}/liquidity-map/?symbol=${encodeURIComponent(symbol)}`}
    className="w-full border-0 rounded-xl"
    style={{ height: "calc(100dvh - 100px)", minHeight: 540 }}
  />;
}
