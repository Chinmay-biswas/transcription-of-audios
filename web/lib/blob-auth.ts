import type { IssueSignedTokenOptions } from "@vercel/blob";

type BlobAuthOptions = Pick<IssueSignedTokenOptions, "oidcToken" | "storeId">;

export function getBlobAuthOptions(request: Request): BlobAuthOptions {
  const requestOidcToken = request.headers.get("x-vercel-oidc-token")?.trim();
  const storeId = process.env.BLOB_STORE_ID?.trim();

  if (requestOidcToken && storeId) {
    return { oidcToken: requestOidcToken, storeId };
  }

  // Keep local development compatible with `vercel env pull` and legacy
  // read/write tokens while requiring request-scoped OIDC in production.
  if (process.env.BLOB_READ_WRITE_TOKEN?.trim()) {
    return {};
  }

  if (process.env.VERCEL_OIDC_TOKEN?.trim() && storeId) {
    return { storeId };
  }

  if (!storeId) {
    throw new Error("BLOB_STORE_ID is missing from this deployment.");
  }

  throw new Error(
    "The x-vercel-oidc-token request header is missing. Enable Secure Backend Access with OIDC Federation for this Vercel project and redeploy."
  );
}
