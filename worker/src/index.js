const REPO = "skosko/auction-watch";
const FILE = "artists.yml";
const GITHUB_API = `https://api.github.com/repos/${REPO}/contents/${FILE}`;
const ALLOWED_ORIGIN = "https://skosko.github.io";

function corsHeaders(origin) {
  return {
    "Access-Control-Allow-Origin": origin === ALLOWED_ORIGIN ? origin : "",
    "Access-Control-Allow-Methods": "GET, PUT, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
  };
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const origin = request.headers.get("Origin") || "";

    // CORS preflight
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders(origin) });
    }

    if (url.pathname !== "/file") {
      return new Response("Not found", { status: 404 });
    }

    if (origin && origin !== ALLOWED_ORIGIN) {
      return new Response("Forbidden", { status: 403 });
    }

    const ghHeaders = {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "User-Agent": "auction-watch-proxy",
    };

    let ghResponse;

    if (request.method === "GET") {
      ghResponse = await fetch(GITHUB_API, { headers: ghHeaders });
    } else if (request.method === "PUT") {
      const body = await request.json();
      ghResponse = await fetch(GITHUB_API, {
        method: "PUT",
        headers: { ...ghHeaders, "Content-Type": "application/json" },
        body: JSON.stringify({
          message: body.message,
          content: body.content,
          sha: body.sha,
        }),
      });
    } else {
      return new Response("Method not allowed", { status: 405 });
    }

    const responseBody = await ghResponse.text();
    return new Response(responseBody, {
      status: ghResponse.status,
      headers: {
        "Content-Type": "application/json",
        ...corsHeaders(origin),
      },
    });
  },
};
