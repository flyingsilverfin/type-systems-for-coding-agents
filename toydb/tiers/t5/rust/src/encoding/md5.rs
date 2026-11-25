//! MD5 digests (RFC 1321), for PostgreSQL's MD5 password method and the stored
//! form of passwords.

/// The per-round shift amounts.
const SHIFTS: [u32; 64] = [
    7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, // round 1
    5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20, // round 2
    4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, // round 3
    6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21, // round 4
];

/// The additive constants: the integer part of abs(sin(i + 1)) * 2^32.
const CONSTANTS: [u32; 64] = [
    0xd76aa478, 0xe8c7b756, 0x242070db, 0xc1bdceee, 0xf57c0faf, 0x4787c62a, 0xa8304613, 0xfd469501,
    0x698098d8, 0x8b44f7af, 0xffff5bb1, 0x895cd7be, 0x6b901122, 0xfd987193, 0xa679438e, 0x49b40821,
    0xf61e2562, 0xc040b340, 0x265e5a51, 0xe9b6c7aa, 0xd62f105d, 0x02441453, 0xd8a1e681, 0xe7d3fbc8,
    0x21e1cde6, 0xc33707d6, 0xf4d50d87, 0x455a14ed, 0xa9e3e905, 0xfcefa3f8, 0x676f02d9, 0x8d2a4c8a,
    0xfffa3942, 0x8771f681, 0x6d9d6122, 0xfde5380c, 0xa4beea44, 0x4bdecfa9, 0xf6bb4b60, 0xbebfbc70,
    0x289b7ec6, 0xeaa127fa, 0xd4ef3085, 0x04881d05, 0xd9d4d039, 0xe6db99e5, 0x1fa27cf8, 0xc4ac5665,
    0xf4292244, 0x432aff97, 0xab9423a7, 0xfc93a039, 0x655b59c3, 0x8f0ccc92, 0xffeff47d, 0x85845dd1,
    0x6fa87e4f, 0xfe2ce6e0, 0xa3014314, 0x4e0811a1, 0xf7537e82, 0xbd3af235, 0x2ad7d2bb, 0xeb86d391,
];

/// Returns the MD5 digest of the data.
pub fn digest(data: &[u8]) -> [u8; 16] {
    let mut state: [u32; 4] = [0x67452301, 0xefcdab89, 0x98badcfe, 0x10325476];

    // Pad the message with a 1 bit, zeroes up to 56 bytes modulo 64, and the
    // message length in bits as a little-endian 64-bit integer.
    let mut message = data.to_vec();
    message.push(0x80);
    while message.len() % 64 != 56 {
        message.push(0);
    }
    message.extend_from_slice(&((data.len() as u64).wrapping_mul(8)).to_le_bytes());

    for block in message.chunks_exact(64) {
        let words: Vec<u32> = block
            .chunks_exact(4)
            .map(|word| u32::from_le_bytes(word.try_into().expect("4-byte word")))
            .collect();
        let [mut a, mut b, mut c, mut d] = state;
        for i in 0..64 {
            let (f, g) = match i / 16 {
                0 => ((b & c) | (!b & d), i),
                1 => ((d & b) | (!d & c), (5 * i + 1) % 16),
                2 => (b ^ c ^ d, (3 * i + 5) % 16),
                _ => (c ^ (b | !d), (7 * i) % 16),
            };
            let rotated = a
                .wrapping_add(f)
                .wrapping_add(CONSTANTS[i])
                .wrapping_add(words[g])
                .rotate_left(SHIFTS[i]);
            (a, b, c, d) = (d, b.wrapping_add(rotated), b, c);
        }
        for (word, added) in state.iter_mut().zip([a, b, c, d]) {
            *word = word.wrapping_add(added);
        }
    }

    let mut digest = [0; 16];
    for (bytes, word) in digest.chunks_exact_mut(4).zip(state) {
        bytes.copy_from_slice(&word.to_le_bytes());
    }
    digest
}

/// Returns the MD5 digest of the data as 32 lower-case hex digits.
pub fn hex(data: &[u8]) -> String {
    digest(data).iter().map(|byte| format!("{byte:02x}")).collect()
}

#[cfg(test)]
mod tests {
    use test_case::test_case;

    use super::*;

    // The test suite of RFC 1321, and padding edge cases.
    #[test_case("" => "d41d8cd98f00b204e9800998ecf8427e"; "empty")]
    #[test_case("a" => "0cc175b9c0f1b6a831c399e269772661"; "a")]
    #[test_case("abc" => "900150983cd24fb0d6963f7d28e17f72"; "abc")]
    #[test_case("message digest" => "f96b697d7cb7938d525a2f31aaf161d0"; "message digest")]
    #[test_case("abcdefghijklmnopqrstuvwxyz" => "c3fcd3d76192e4007dfb496cca67e13b"; "alphabet")]
    #[test_case("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
        => "d174ab98d277d9f5a5611c2c9f419d9f"; "alphanumeric")]
    #[test_case("12345678901234567890123456789012345678901234567890123456789012345678901234567890"
        => "57edf4a22be3c955ac49da2e2107b67a"; "digits")]
    #[test_case(&"x".repeat(55) => "04364420e25c512fd958a70738aa8f72"; "55 bytes")]
    #[test_case(&"x".repeat(56) => "668a72d5ba17f08e62dabcafad6db14b"; "56 bytes")]
    #[test_case(&"x".repeat(64) => "c1bb4f81d892b2d57947682aeb252456"; "64 bytes")]
    fn md5(input: &str) -> String {
        hex(input.as_bytes())
    }

    #[test]
    fn password() {
        // The digest of the password pw followed by the user name alice.
        assert_eq!(hex(b"pwalice"), "aa7bb6fc2700e717ae61ec5c1567469c");
    }
}
