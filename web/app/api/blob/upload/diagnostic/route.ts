import { issueSignedToken } from "@vercel/blob";
import { NextResponse } from "next/server";

import { getBlobAuthOptions } from "@/lib/blob-auth";

export const dynamic = "force-dynamic";

const noStoreHeaders = { "Cache-Control": "no-store" };

function getSafeSetupMessage(error: unknown): string {
  if (!process.env.BLOB_STORE_ID?.trim()) {
    return "Vercel Blob is not connected to this deployment. Connect the Blob store to this Vercel project, then redeploy.";
  }

  if (!process.env.BLOB_WEBHOOK_PUBLIC_KEY?.trim()) {
    return "The connected Vercel Blob store is missing its webhook public key. Reconnect the store to this Vercel project, then redeploy.";
  }

  const detail = error instanceof Error ? error.message.toLowerCase() : "";
  if (detail.includes("no blob credentials") || detail.includes("oidc")) {
    return "Vercel OIDC is unavailable. In Project Settings > Security, enable Secure Backend Access with OIDC Federation, save, and redeploy.";
  }

  if (/not.authorized|unauthorized|forbidden|\b401\b|\b403\b|\b404\b/.test(detail)) {
    return "Vercel denied access to this Blob store. In Storage > your Blob store > Projects, reconnect this Vercel project, then redeploy.";
  }

  return "Vercel Blob could not be initialized. Open the latest deployment's Runtime Logs and look for the /api/blob/upload request.";
}

export async function GET(request: Request): Promise<NextResponse> {
  try {
    const blobAuth = getBlobAuthOptions(request);
    await issueSignedToken({
      ...blobAuth,
      pathname: "meetings/__connection-check__.mp3",
      operations: ["put"],
      validUntil: Date.now() + 60_000
    });

    return NextResponse.json({ ready: true }, { headers: noStoreHeaders });
  } catch (error) {
    const message = getSafeSetupMessage(error);
    console.error("Vercel Blob setup check failed:", message);
    return NextResponse.json({ error: message }, { status: 503, headers: noStoreHeaders });
  }
}
