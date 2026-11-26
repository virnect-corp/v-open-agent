package ai.voa.client

import ai.voa.client.output.RenderSpec
import ai.voa.client.output.StatusKey
import ai.voa.client.output.StatusRow
import ai.voa.client.protocol.FlashMode
import android.content.Context
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Handler
import android.os.Looper
import android.text.TextUtils
import android.view.Gravity
import android.view.View
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.widget.FrameLayout
import android.widget.TextView

/**
 * The glasses display area drawn over the camera view (SPEC 7.4): a popup for alerts and
 * notices, a full-screen or border flash, and the status bar. No detection boxes.
 * A 5% safe margin keeps text away from the edges.
 */
class HudLayer(context: Context) : FrameLayout(context) {
    private val main = Handler(Looper.getMainLooper())
    private val density = resources.displayMetrics.density
    private val flash = View(context).apply { visibility = INVISIBLE }
    private val safe = FrameLayout(context)
    private val popup = TextView(context).apply {
        visibility = GONE
        ellipsize = TextUtils.TruncateAt.END
        gravity = Gravity.CENTER
        val pad = (12 * density).toInt()
        setPadding(pad * 2, pad, pad * 2, pad)
    }
    private val status = TextView(context).apply {
        setTextColor(Color.WHITE)
        textSize = 14f
        val pad = (6 * density).toInt()
        setPadding(pad * 2, pad, pad * 2, pad)
        background = GradientDrawable().apply {
            cornerRadius = 999f
            setColor(0x8C000000.toInt())
        }
    }
    private var hideAt: Runnable? = null
    private val flashSteps = mutableListOf<Runnable>()

    init {
        addView(flash, LayoutParams(MATCH_PARENT, MATCH_PARENT))
        addView(safe, LayoutParams(MATCH_PARENT, MATCH_PARENT))
        safe.addView(status, LayoutParams(WRAP_CONTENT, WRAP_CONTENT, Gravity.TOP or Gravity.END))
        safe.addView(popup, LayoutParams(WRAP_CONTENT, WRAP_CONTENT, Gravity.TOP or Gravity.CENTER_HORIZONTAL))
        addOnLayoutChangeListener { _, l, t, r, b, _, _, _, _ ->
            val x = ((r - l) * 0.05f).toInt()
            val y = ((b - t) * 0.05f).toInt()
            safe.setPadding(x, y, x, y)
        }
    }

    /** Show a reduced presentation now; returns at display start (`played` time). */
    fun show(spec: RenderSpec) {
        hideAt?.let { main.removeCallbacks(it) }
        popup.text = spec.text
        popup.maxLines = spec.maxLines
        popup.textSize = if (spec.large) 26f else 20f
        popup.setTypeface(Typeface.DEFAULT, if (spec.large) Typeface.BOLD else Typeface.NORMAL)
        val color = spec.color?.let { colorOf(it) }
        popup.setTextColor(color ?: Color.BLACK)
        popup.background = GradientDrawable().apply {
            cornerRadius = 12 * density
            setColor(if (color == null) Color.WHITE else 0x99000000.toInt()) // mono: inverse emphasis
        }
        popup.layoutParams = (popup.layoutParams as LayoutParams).apply {
            gravity = when (spec.position) {
                "center" -> Gravity.CENTER
                "bottom_center" -> Gravity.BOTTOM or Gravity.CENTER_HORIZONTAL
                else -> Gravity.TOP or Gravity.CENTER_HORIZONTAL
            }
            topMargin = if (spec.position == "top_center") (40 * density).toInt() else 0 // below the status bar
        }
        popup.visibility = VISIBLE
        startFlash(spec, color ?: Color.WHITE)
        hideAt = Runnable { popup.visibility = GONE }.also { main.postDelayed(it, spec.showMs.toLong()) }
    }

    fun hide() {
        hideAt?.let { main.removeCallbacks(it) }
        popup.visibility = GONE
        stopFlash()
    }

    fun setStatus(row: StatusRow) {
        val dot = when (row.key.tone) {
            "green-dot" -> "● "
            "yellow-dot", "yellow" -> "● "
            "red" -> "● "
            else -> "○ "
        }
        status.text = dot + row.key.label
        status.setTextColor(
            when (row.key) {
                StatusKey.NORMAL -> 0xFF34C759.toInt()
                StatusKey.CANDIDATE, StatusKey.SLOW -> 0xFFFFCC00.toInt()
                StatusKey.WARNING, StatusKey.SERVER_ERROR -> 0xFFFF6B61.toInt()
                else -> 0xFFC8CCD2.toInt()
            },
        )
    }

    private fun startFlash(spec: RenderSpec, color: Int) {
        stopFlash()
        if (spec.flashMode == FlashMode.NONE || spec.flashCount <= 0) return
        flash.background = if (spec.flashMode == FlashMode.FULL_SCREEN) {
            GradientDrawable().apply { setColor((color and 0x00FFFFFF) or 0x73000000) }
        } else {
            GradientDrawable().apply { setStroke((8 * density).toInt(), color) }
        }
        val period = (spec.onMs + spec.offMs).toLong()
        repeat(spec.flashCount) { i ->
            val on = Runnable { flash.visibility = VISIBLE }
            val off = Runnable { flash.visibility = INVISIBLE }
            flashSteps += on
            flashSteps += off
            main.postDelayed(on, i * period)
            main.postDelayed(off, i * period + spec.onMs)
        }
    }

    private fun stopFlash() {
        flashSteps.forEach { main.removeCallbacks(it) }
        flashSteps.clear()
        flash.visibility = INVISIBLE
    }

    private fun colorOf(name: String): Int = when (name) {
        "red" -> 0xFFFF3B30.toInt()
        "yellow" -> 0xFFFFCC00.toInt()
        else -> Color.WHITE
    }
}
