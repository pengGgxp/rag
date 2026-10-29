import type { Link, Parent, Root, RootContent } from "mdast";

export function citationMarkers(citations: readonly { chunk_id: string }[]) {
  const sources = new Map(citations.map((citation, index) => [citation.chunk_id, index + 1]));
  return () => (tree: Root) => {
    function visit(parent: Parent) {
      const children: RootContent[] = [];
      for (const child of parent.children) {
        if (child.type === "text") {
          const marker = /\[\s*(?:chunk_id\s*[:：]\s*)?([^\]\s]+)\s*\]/gi;
          let offset = 0;
          for (const match of child.value.matchAll(marker)) {
            const number = sources.get(match[1]);
            if (number === undefined) continue;
            if (match.index > offset) children.push({ type: "text", value: child.value.slice(offset, match.index) });
            const link: Link = { type: "link", url: `#source-${match[1]}`,
              children: [{ type: "text", value: String(number) }] };
            children.push(link);
            offset = match.index + match[0].length;
          }
          if (offset === 0) children.push(child);
          else if (offset < child.value.length) children.push({ type: "text", value: child.value.slice(offset) });
        } else {
          // Only prose is eligible; existing links and code keep their original meaning.
          if ("children" in child && !["link", "linkReference", "image", "imageReference"].includes(child.type))
            visit(child);
          children.push(child);
        }
      }
      parent.children = children;
    }
    visit(tree);
  };
}
