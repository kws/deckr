import { readFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const CONTRACT_ARTIFACT_VERSION = "v1";

export interface ContractArtifact {
  id: string;
  kind: string;
  path: string;
  title?: string;
  description?: string;
  schemaPath?: string;
  valid?: boolean;
  protocol?: string;
}

export interface ContractManifest {
  bundle: string;
  contractVersion: string;
  specVersion: string;
  deckrPackageVersion: string;
  generatedAt: string;
  artifacts: ContractArtifact[];
}

export function defaultContractRoot(version = CONTRACT_ARTIFACT_VERSION): string {
  const startPoints = [
    dirname(fileURLToPath(import.meta.url)),
    process.cwd(),
  ];
  for (const start of startPoints) {
    for (const base of ancestors(start)) {
      const candidate = join(base, "contract", version);
      if (existsSync(join(candidate, "manifest.json"))) {
        return candidate;
      }
    }
  }
  throw new Error(`could not find contract/${version} from the TypeScript source checkout`);
}

export async function readJsonFile<T = unknown>(path: string): Promise<T> {
  return JSON.parse(await readFile(path, "utf8")) as T;
}

export async function readJsonArtifact<T = unknown>(
  relativePath: string,
  contractRoot = defaultContractRoot(),
): Promise<T> {
  return readJsonFile<T>(join(contractRoot, relativePath));
}

export async function loadManifest(
  contractRoot = defaultContractRoot(),
): Promise<ContractManifest> {
  return readJsonFile<ContractManifest>(join(contractRoot, "manifest.json"));
}

function* ancestors(start: string): Iterable<string> {
  let current = resolve(start);
  while (true) {
    yield current;
    const parent = dirname(current);
    if (parent === current) {
      return;
    }
    current = parent;
  }
}
