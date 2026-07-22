import { useEffect, useState } from "react";
import { KeyRound, LogIn } from "lucide-react";
import { Button, Input, Modal } from "antd";
import { useAuth } from "../auth";

export function AccessDialog() {
  const { accessOpen, closeAccess, connect, operatorKey } = useAuth();
  const [value, setValue] = useState("");

  useEffect(() => {
    if (accessOpen) setValue("");
  }, [accessOpen]);

  return (
    <Modal
      open={accessOpen}
      title={<span className="access-title"><span className="brand-mark">N</span>Operator access</span>}
      closable={Boolean(operatorKey)}
      maskClosable={Boolean(operatorKey)}
      onCancel={closeAccess}
      footer={null}
      width={420}
      centered
    >
      <form
        className="access-form"
        onSubmit={(event) => {
          event.preventDefault();
          if (value.trim()) connect(value.trim());
        }}
      >
        <label htmlFor="operator-key">Operator key</label>
        <Input.Password
          id="operator-key"
          prefix={<KeyRound size={15} />}
          value={value}
          onChange={(event) => setValue(event.target.value)}
          autoFocus
          autoComplete="current-password"
        />
        <Button type="primary" htmlType="submit" icon={<LogIn size={15} />} disabled={!value.trim()}>
          Connect
        </Button>
      </form>
    </Modal>
  );
}
