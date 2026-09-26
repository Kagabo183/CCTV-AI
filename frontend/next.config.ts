import type { NextConfig } from "next";

// The browser only ever talks to this Next.js origin. /api/* is proxied to the
// FastAPI backend, so the session cookie is first-party and no provider keys or
// backend URLs are exposed to the client.
const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";
const MEDIA_WEBRTC_URL = process.env.MEDIA_WEBRTC_URL ?? "http://127.0.0.1:8889";
const MEDIA_HLS_URL = process.env.MEDIA_HLS_URL ?? "http://127.0.0.1:8888";

const nextConfig: NextConfig = {
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${BACKEND_URL}/api/:path*` },
      // Live camera video from the media server (WebRTC/WHEP signalling and HLS). Every request carries a
      // short-lived token for one camera path; the media server checks it with the backend.
      { source: "/media/webrtc/:path*", destination: `${MEDIA_WEBRTC_URL}/:path*` },
      { source: "/media/hls/:path*", destination: `${MEDIA_HLS_URL}/:path*` },
    ];
  },
  experimental: {
    // Video analysis can take longer than the default 30s proxy timeout.
    proxyTimeout: 300_000,
    // The proxy buffers request bodies in memory and truncates past this limit,
    // so it must exceed the backend upload cap (VIDEO_MAX_UPLOAD_MB=500).
    proxyClientMaxBodySize: "520mb",
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Permissions-Policy", value: "camera=(), geolocation=(), microphone=(self)" },
        ],
      },
    ];
  },
};

export default nextConfig;
