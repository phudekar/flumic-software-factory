import flumicIcon from "../assets/flumic.svg";
import { LoaderCircle, ShieldCheck } from "lucide-react";
import { useState } from "react";
import { api, setCsrf } from "../api";
import { Failure } from "../components/ui";

export function Login({ onLogin }: { onLogin: () => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  return (
    <div className="login">
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          setPending(true);
          try {
            const result = await api<{ csrf: string }>("/auth/login", {
              password,
            });
            setCsrf(result.csrf);
            onLogin();
          } catch (error) {
            setError((error as Error).message);
          } finally {
            setPending(false);
          }
        }}
      >
        <div className="brand-icon">
          <img src={flumicIcon} alt="" aria-hidden="true" />
        </div>
        <span className="eyebrow">FLUMIC AI</span>
        <h1>
          Your factory,
          <br />
          in focus.
        </h1>
        <p>Sign in to manage workers and follow work from issue to delivery.</p>
        <label>
          Administrator password
          <input
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
        </label>
        <Failure error={error} />
        <button className="primary" disabled={pending}>
          {pending ? (
            <LoaderCircle className="spin" size={16} />
          ) : (
            <ShieldCheck size={16} />
          )}
          Sign in
        </button>
        <small>Private control plane · Credentials stay on the server</small>
      </form>
    </div>
  );
}
