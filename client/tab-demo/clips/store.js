const clipPattern = /^(\d+)_([\w-]+)_([\w-]+)_[\w-]+\.webm$/;

export async function clipDirectory() {
  const root = await navigator.storage.getDirectory();
  return root.getDirectoryHandle("encore-tab-clips", {create: true});
}

export async function savedClips(dir) {
  const result = [];
  for await (const [name, handle] of dir.entries()) {
    const match = name.match(clipPattern);
    if (match && handle.kind === "file") {
      let meta;
      try {
        const sidecar = await dir.getFileHandle(name + ".json");
        meta = JSON.parse(await (await sidecar.getFile()).text());
      } catch (err) { if (err.name !== "NotFoundError") throw err; }
      result.push({handle, meta, time: Number(match[1]), id: match[2], reaction: match[3]});
    }
  }
  return result.sort((a, b) => b.time - a.time || (a.meta?.file_index ?? 1) - (b.meta?.file_index ?? 1));
}
