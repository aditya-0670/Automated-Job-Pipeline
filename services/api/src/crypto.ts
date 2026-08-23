/**
 * Encryption for the one secret this service stores on behalf of a user: their
 * GitHub PAT.
 *
 * A PAT is not a password — it is a bearer credential this service must be able
 * to *use*, so it cannot be hashed. That leaves reversible encryption, and the
 * only honest thing to say about it is what it does and does not protect
 * against: a stolen database dump is useless without `ENCRYPTION_KEY`, and a
 * compromised application process is not protected at all.
 *
 * AES-256-GCM, so the ciphertext is authenticated. CBC would decrypt tampered
 * input into plausible garbage that then gets sent to GitHub as a credential.
 */

import { createCipheriv, createDecipheriv, randomBytes, scryptSync } from "node:crypto";

import { getConfig } from "./config.js";

const ALGORITHM = "aes-256-gcm";
const IV_BYTES = 12; // 96 bits, the GCM standard
const TAG_BYTES = 16;
/** A fixed salt is acceptable here and nowhere near a password: the input is a
 *  high-entropy configured key, not something a human chose, so the salt is only
 *  domain separation, not protection against precomputation. */
const KEY_SALT = "resumeforge-token-encryption";

let cachedKey: Buffer | undefined;

function key(): Buffer {
  if (!cachedKey) {
    cachedKey = scryptSync(getConfig().ENCRYPTION_KEY, KEY_SALT, 32);
  }
  return cachedKey;
}

/**
 * Encrypt to `v1.<iv>.<tag>.<ciphertext>`, all base64url.
 *
 * The version prefix is the point of the format: rotating to a new algorithm or
 * key later needs a way to tell which scheme produced a given row, and adding
 * that after the fact means guessing.
 */
export function encryptSecret(plaintext: string): string {
  const iv = randomBytes(IV_BYTES);
  const cipher = createCipheriv(ALGORITHM, key(), iv);
  const ciphertext = Buffer.concat([cipher.update(plaintext, "utf8"), cipher.final()]);
  const tag = cipher.getAuthTag();
  return ["v1", iv.toString("base64url"), tag.toString("base64url"), ciphertext.toString("base64url")].join(".");
}

export function decryptSecret(payload: string): string {
  const [version, ivPart, tagPart, dataPart] = payload.split(".");
  if (version !== "v1" || !ivPart || !tagPart || !dataPart) {
    throw new Error("Unrecognised encrypted value");
  }
  const iv = Buffer.from(ivPart, "base64url");
  const tag = Buffer.from(tagPart, "base64url");
  if (iv.length !== IV_BYTES || tag.length !== TAG_BYTES) {
    throw new Error("Malformed encrypted value");
  }
  const decipher = createDecipheriv(ALGORITHM, key(), iv);
  decipher.setAuthTag(tag);
  // Throws on a bad tag, which is the whole reason for GCM: tampered input
  // fails loudly instead of decrypting into something that looks like a token.
  return Buffer.concat([decipher.update(Buffer.from(dataPart, "base64url")), decipher.final()]).toString("utf8");
}

/** Test hook, for a changed key. */
export function resetKeyCache(): void {
  cachedKey = undefined;
}
