import { createContext, useContext, useMemo, useState, type ReactNode } from "react";

const KEY_STORAGE = "northgate.operatorKey";

interface AuthValue {
  operatorKey: string;
  accessOpen: boolean;
  openAccess: () => void;
  closeAccess: () => void;
  connect: (key: string) => void;
  disconnect: () => void;
}

const AuthContext = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [operatorKey, setOperatorKey] = useState(() => sessionStorage.getItem(KEY_STORAGE) ?? "");
  const [accessOpen, setAccessOpen] = useState(!operatorKey);
  const value = useMemo<AuthValue>(() => ({
    operatorKey,
    accessOpen,
    openAccess: () => setAccessOpen(true),
    closeAccess: () => setAccessOpen(false),
    connect: (key: string) => {
      sessionStorage.setItem(KEY_STORAGE, key);
      setOperatorKey(key);
      setAccessOpen(false);
    },
    disconnect: () => {
      sessionStorage.removeItem(KEY_STORAGE);
      setOperatorKey("");
      setAccessOpen(true);
    },
  }), [accessOpen, operatorKey]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside AuthProvider");
  return value;
}
