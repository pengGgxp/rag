import { SkillEditor } from "../skill-editor";
export default async function SkillDetailPage({ params }: { params: Promise<{ id: string }> }) {
  return <SkillEditor id={(await params).id} />;
}
