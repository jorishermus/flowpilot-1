package ai.flow.common;

public class utils {
    public static boolean getBoolEnvVar(String key) {
        String val = System.getenv(key);
        boolean ret = false;
        if (val != null) {
            if (val.equals("1"))
                ret = true;
        }
        return ret;
    }

    public static double secSinceBoot() {
        return System.currentTimeMillis() / 1000.0;
    }

    public static double milliSinceBoot() {
        return System.currentTimeMillis();
    }

    public static long nanoSinceBoot() {
        return System.nanoTime();
    }
}