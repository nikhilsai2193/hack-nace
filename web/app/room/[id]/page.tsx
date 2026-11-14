"use client";

import { useParams } from "next/navigation";
import StudyRoom from "@/components/StudyRoom";

export default function RoomPage() {
  const params = useParams<{ id: string }>();
  return <StudyRoom roomId={params.id} />;
}
