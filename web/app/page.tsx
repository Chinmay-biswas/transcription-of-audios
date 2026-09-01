import Link from "next/link";

const features = [
  {
    number: "01",
    title: "Upload once",
    description: "Large meeting recordings upload directly to durable Blob storage."
  },
  {
    number: "02",
    title: "Extract signal",
    description: "Whisper transcription and Gemini analysis produce clear decisions and tasks."
  },
  {
    number: "03",
    title: "Ask later",
    description: "Qdrant keeps your meeting knowledge searchable across sessions."
  }
];

export default function HomePage() {
  return (
    <section className="hero">
      <p className="eyebrow">Meeting operations, made searchable</p>
      <h1>Turn every recorded conversation into a useful next step.</h1>
      <p className="hero-copy">
        Upload an audio recording, receive a structured meeting brief, and return later
        to ask exactly what was decided.
      </p>
      <div className="hero-actions">
        <Link href="/upload" className="button button-primary">
          Process a recording
        </Link>
        <Link href="/meetings" className="button button-secondary">
          Explore meetings
        </Link>
      </div>

      <div className="feature-grid">
        {features.map((feature) => (
          <article className="feature-card" key={feature.number}>
            <span>{feature.number}</span>
            <h2>{feature.title}</h2>
            <p>{feature.description}</p>
          </article>
        ))}
      </div>
    </section>
  );
}
