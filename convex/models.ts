import { openai } from "@ai-sdk/openai";
import { convexGateway } from "@convex-dev/ai-sdk-provider";

export const EMBEDDING_DIMENSION = 1536;

// Chat and embeddings use the Convex AI Gateway, which holds provider
// credentials. Set LINKEDEYE_AI_GATEWAY=off only when that gateway is
// unavailable (free plan, self-hosted, or an anonymous local backend).
// The fallback reads OPENAI_API_KEY from the Convex deployment env
// (`npx convex env set`); do not commit the value.
export function gatewayOff(): boolean {
  return process.env.LINKEDEYE_AI_GATEWAY === "off";
}

export function chatModel() {
  if (gatewayOff()) {
    return openai.chat("gpt-4o-mini");
  }
  return convexGateway("openai/gpt-4o-mini");
}

export function textEmbeddingModel() {
  if (gatewayOff()) {
    return openai.embedding("text-embedding-3-small");
  }
  return convexGateway.embeddingModel("openai/text-embedding-3-small");
}
