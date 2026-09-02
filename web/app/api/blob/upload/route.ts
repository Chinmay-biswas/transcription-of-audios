import { issueSignedToken } from "@vercel/blob";
import {
  handleUploadPresigned,
  type HandleUploadPresignedBody
} from "@vercel/blob/client";
import { NextResponse } from "next/server";

import { getBlobAuthOptions } from "@/lib/blob-auth";

const supportedExtensions = new Set(["mp3", "wav", "m4a", "mp4", "mov", "webm"]);
const supportedContentTypes = [
  "audio/mpeg",
  "audio/mp3",
  "audio/wav",
  "audio/x-wav",
  "audio/mp4",
  "audio/x-m4a",
  "audio/m4a",
  "audio/webm",
  "video/mp4",
  "video/quicktime",
  "video/webm"
];
const defaultMaxMediaBytes = 2 * 1024 * 1024 * 1024;

function getMaxMediaBytes(): number {
  const configured = process.env.MAX_MEDIA_BYTES?.trim();
  if (!configured) {
    return defaultMaxMediaBytes;
  }

  const bytes = Number(configured);
  if (!Number.isSafeInteger(bytes) || bytes <= 0) {
    throw new Error("MAX_MEDIA_BYTES must be a positive whole number.");
  }
  return bytes;
}

export async function POST(request: Request): Promise<NextResponse> {
  try {
    const blobAuth = getBlobAuthOptions(request);
    const maxMediaBytes = getMaxMediaBytes();
    const body = (await request.json()) as HandleUploadPresignedBody;
    const jsonResponse = await handleUploadPresigned({
      body,
      request,
      getSignedToken: async (pathname) => {
        const extension = pathname.split(".").pop()?.toLowerCase();
        if (!pathname.startsWith("meetings/") || !extension || !supportedExtensions.has(extension)) {
          throw new Error("Only MP3, WAV, M4A, MP4, MOV, and WebM meeting recordings are accepted.");
        }

        return {
          token: await issueSignedToken({
            ...blobAuth,
            pathname,
            operations: ["put"],
            allowedContentTypes: supportedContentTypes,
            maximumSizeInBytes: maxMediaBytes,
            validUntil: Date.now() + 15 * 60 * 1000
          }),
          urlOptions: {
            allowedContentTypes: supportedContentTypes,
            maximumSizeInBytes: maxMediaBytes,
            addRandomSuffix: true,
            tokenPayload: JSON.stringify({ purpose: "meeting-audio" })
          }
        };
      }
    });

    return NextResponse.json(jsonResponse);
  } catch (error) {
    const message = error instanceof Error ? error.message : "Could not prepare the upload.";
    return NextResponse.json({ error: message }, { status: 400 });
  }
}
