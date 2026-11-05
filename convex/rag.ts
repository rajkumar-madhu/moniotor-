import { RAG } from "@convex-dev/rag";
import { components } from "./_generated/api";
import { EMBEDDING_DIMENSION, textEmbeddingModel } from "./models";

type RunbookFilters = {
  site: string;
};

export function monitoringRag() {
  return new RAG<RunbookFilters>(components.rag, {
    textEmbeddingModel: textEmbeddingModel(),
    embeddingDimension: EMBEDDING_DIMENSION,
    filterNames: ["site"],
  });
}
