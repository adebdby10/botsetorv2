"""Apply all OTP flow fixes to handler.py using line-based replacement."""
with open('bot/handler.py', 'r') as f:
    lines = f.readlines()

# Find the phone input block and replace
changes = 0
new_lines = []
skip_mode = False
in_phone_block = False
phone_block_end = False

for i, line in enumerate(lines):
    # Detect start of STATE_WAIT_UB_PHONE + "client = TelegramClient"
    if '            client = TelegramClient(str(session_path), api_id, api_hash)' in line:
        lines[i] = '            from telethon.sessions import StringSession\n'
        lines[i] += '            ub_session = StringSession()\n'
        lines[i] += '            client = TelegramClient(ub_session, api_id, api_hash)\n'
        changes += 1
        print(f"Line {i+1}: Changed to StringSession")

    # Add ub_session_path after ub_phone_code_hash
    if 'st["ub_phone_code_hash"] = result.phone_code_hash' in line:
        lines[i] = line.rstrip() + '\n'
        lines[i] += '                st["ub_session_path"] = str(session_path)\n'
        changes += 1
        print(f"Line {i+1}: Added ub_session_path")

    # Add logging after send_code_request success
    if 'st["state"] = STATE_WAIT_UB_OTP' in line and i < 580:  # phone block
        indent = ' ' * 16
        lines[i] = line.rstrip() + '\n'
        lines[i] += f'{indent}print(f"📱 OTP request sent to {{phone}} (hash={{result.phone_code_hash[:10]}}...)")\n'
        changes += 1
        print(f"Line {i+1}: Added OTP log")

    # Add logging for error
    if '❌ Gagal kirim OTP:' in line and 'api_id' not in lines[max(0,i-2)]:
        # Already has this, skip
        pass

print("=" * 40)

# Now fix the OTP input block (STATE_WAIT_UB_OTP sign_in)
# Need to add phone_code_hash to sign_in call and verbose logging
for i, line in enumerate(lines):
    # Find sign_in line in OTP block
    if 'await client.sign_in(phone=phone, code=code)' in line:
        lines[i] = '                await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)\n'
        changes += 1
        print(f"Line {i+1}: Added phone_code_hash to sign_in")
        
        # Add hash retrieval before try block
        # Find the try: before this line
        for j in range(i-5, i):
            if 'try:' in lines[j]:
                indent = ' ' * 12
                lines[j] = lines[j].rstrip() + '\n'
                lines[j] += f'{indent}phone_code_hash = st.get("ub_phone_code_hash")\n'
                lines[j] += f'{indent}print(f"🔐 sign_in: phone={{phone}}, code={{code}}, hash={{phone_code_hash[:10] if phone_code_hash else \'NONE\'}}...")\n'
                changes += 1
                print(f"Line {j+1}: Added hash retrieval and log")
                break
        break

# Fix the OTP success block - add session save
for i, line in enumerate(lines):
    if 'await client.disconnect()' in line:
        # Check if this is in the OTP success path (not 2FA path)
        # Check 2-3 lines before
        before = ''.join(lines[max(0,i-5):i])
        if 'Sukses login' in before or 'me = await' in before:
            indent = ' ' * 16
            lines[i] = f'{indent}# Save StringSession -> .session file\n'
            lines[i] += f'{indent}api_id_save, api_hash_save = get_api()\n'
            lines[i] += f'{indent}session_path = Path(session_path_str) if session_path_str else None\n'
            lines[i] += f'{indent}if session_path:\n'
            lines[i] += f'{indent}    from telethon import TelegramClient as _TC\n'
            lines[i] += f'{indent}    ss_cl = _TC(str(session_path), api_id_save, api_hash_save)\n'
            lines[i] += f'{indent}    ss_cl.session._dc_id = client.session._dc_id\n'
            lines[i] += f'{indent}    ss_cl.session._server_address = client.session._server_address\n'
            lines[i] += f'{indent}    ss_cl.session._port = client.session._port\n'
            lines[i] += f'{indent}    ss_cl.session._auth_key = client.session._auth_key\n'
            lines[i] += f'{indent}    await ss_cl.connect()\n'
            lines[i] += f'{indent}    await ss_cl.disconnect()\n'
            lines[i] += f'{indent}    print(f"💾 Session saved: {{session_path.name}}")\n'
            lines[i] += f'\n{indent}await client.disconnect()\n'
            changes += 1
            print(f"Line {i+1}: Added session save after login success")
            break

# Add session_path_str retrieval in OTP input block
for i, line in enumerate(lines):
    if 'client = st.get("ub_client")' in line:
        idx = i + 1
        indent = ' ' * 12
        lines[idx] = lines[idx].rstrip() + '\n'
        lines[idx] += f'{indent}session_path_str = st.get("ub_session_path")\n'
        changes += 1
        print(f"Line {idx}: Added session_path_str retrieval")
        break

with open('bot/handler.py', 'w') as f:
    f.writelines(lines)

print(f"\n✅ Total changes: {changes}")
