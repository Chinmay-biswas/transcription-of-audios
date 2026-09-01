import { handleUpload, type HandleUploadBody } from "@vercel/blob/client";
import { NextResponse } from "next/server";

const supportedExtensions = new Set(["mp3", "wav", "m4a"]);
const supportedContentTypes = [
  "audio/mpeg",
  "audio/mp3",
  "audio/wav",
  "audio/x-wav",
  "audio/mp4",
  "audio/x-m4a",
  "audio/m4a"
];

export async function POST(request: Request): Promise<NextResponse> {
  try {
    const body = (await request.json()) as HandleUploadBody;
    const jsonResponse = await handleUpload({
      body,
      request,
      onBeforeGenerateToken: async (pathname) => {
        const extension = pathname.split(".").pop()?.toLowerCase();
        if (!pathname.startsWith("meetings/") || !extension || !supportedExtensions.has(extension)) {
          throw new Error("Only MP3, WAV, and M4A meeting recordings are accepted.");
        }

        return {
          allowedContentTypes: supportedContentTypes,
          maximumSizeInBytes: 100 * 1024 * 1024,
          addRandomSuffix: true,
          tokenPayload: JSON.stringify({ purpose: "meeting-audio" })
        };
      },
      onUploadCompleted: async () => {
        // Processing is initiated by the browser after the direct upload completes.
      }
    });

    return NextResponse.json(jsonResponse);
  } catch (error) {
    const message = error instanceof Error ? error.message : "Could not prepare the upload.";
    return NextResponse.json({ error: message }, { status: 400 });
  }
}
