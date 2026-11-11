// Cloudflare Worker: ye tumhare Render bot ke aage "front door" ban jata hai.
// Isse tumhe milta hai:  xxx.workers.dev  ya  apna custom domain (sites.mydomain.com / *.mydomain.com)
// + Cloudflare ka free CDN cache (Render par load kam).
//
// SETUP: neeche ORIGIN mein apna Render link daalo (bina / ke), phir Deploy dabao.

const ORIGIN = "https://YOUR-APP.onrender.com";

export default {
  async fetch(request, env) {
    const origin = new URL(env.ORIGIN || ORIGIN);
    const url = new URL(request.url);

    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("Method not allowed", { status: 405 });
    }

    const target = new URL(url.pathname, origin);
    // Cache key alag-alag subdomains ke liye alag rakhne ke liye (wildcard mode)
    target.searchParams.set("__h", url.host);

    const headers = new Headers(request.headers);
    headers.set("X-Original-Host", url.host);        // bot is header se subdomain pehchanta hai

    const isHealth = url.pathname === "/health";
    const resp = await fetch(target.toString(), {
      method: request.method,
      headers,
      redirect: "follow",
      cf: isHealth ? { cacheTtl: 0 } : {
        cacheEverything: true,
        cacheTtlByStatus: { "200-299": 60, "404": 10, "500-599": 0 },
      },
    });

    // Render free sleep se jag raha ho to pehla request slow ho sakta hai, ye normal hai.
    return new Response(resp.body, resp);
  },
};
